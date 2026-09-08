"""技能封面图:部门默认图,以及作者上传的那张图的校验与归一化。

## 为什么要重新编码,而不是把用户发来的字节原样存下

这张图最终由门户**同源**返回,所以它同时是一个内容注入面。只看扩展名或客户端声明的
``Content-Type`` 挡不住 polyglot 文件(前面是合法的 PNG 头、后面挂一段 HTML 或脚本)。
经 Pillow 解码再重新编码之后,存进库的字节是**我们生成的**,而不是用户给的 —— 夹带的尾部数据、
EXIF(可能带 GPS 坐标)、以及畸形结构一并消失。

顺带解决第二件事:上传者不必关心尺寸。1024 像素以内足够卡片和详情页用,而一张 4000 像素的
手机照片会让每次卡片渲染都拖几 MB。

## 为什么不收 SVG

SVG 是可执行的 XML,同源返回等于存储型 XSS,而且它**没法靠重新编码消毒** —— 消毒 SVG 要写
一个 XML 白名单过滤器,那是另一个量级的攻击面。PNG / JPEG / WebP 覆盖了实际需求。
"""
from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError

#: 单张图的上限。端点按块读并累计,超了就 413。
#:
#: 512 KB 是"归一化之后的一张 1024px 截图绰绰有余"和"库里别放大东西"之间的取值。注意这个闸门
#: 拦的是**进程内存**:python-multipart 在处理函数跑起来之前就已经把整个请求体落到临时文件了,
#: 真正意义上的传输上限属于边缘(nginx 的 client_max_body_size),不在这里。
MAX_UPLOAD_BYTES = 512 * 1024

#: 长边上限,等比缩。比这个小的图不放大。
MAX_DIMENSION = 1024

#: 收哪几种。键是 Pillow 报的格式名,值是回吐时用的 MIME。
#: 顺序无所谓,但**存下来的格式与传入的格式相同** —— 不做统一转换,免得把一张带透明通道的
#: PNG 压成白底 JPEG。
ALLOWED_FORMATS = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}

#: 重新编码时用的色彩模式。JPEG 不支持 alpha,带透明通道的图存成 JPEG 会直接抛,所以显式转 RGB。
_SAVE_MODE = {"PNG": "RGBA", "WEBP": "RGBA", "JPEG": "RGB"}

#: 解压炸弹闸门:一个几 KB 的文件可以声明成上亿像素,解码时把内存吃光。Pillow 自带一个默认阈值
#: 但只发 warning;这里显式压到一个和 MAX_DIMENSION 同量级的值,并把它当错误处理(见 normalize)。
Image.MAX_IMAGE_PIXELS = 64 * 1024 * 1024 // 4  # 16M 像素,约 4000x4000


class InvalidImage(ValueError):
    """这些字节不是我们愿意存的图。消息是给上传者看的,所以说清哪里不行。"""


#: 在前端 ``public/skill-images/`` 里真的有 ``<slug>.jpg`` 的分组。
#:
#: 写成显式集合而不是"所有分组"是因为这两边住在不同的仓:图片在前端仓、分组在后端代码里,
#: 加一个分组不会自动带来一张图。而缺图的后果是卡片指向 404、显示破图但不报错,所以只有
#: 显式列出来才能让"漏了图"退化成兜底图而不是破图。
#:
#: 刻意不含 ``others``(兜底分组,不代表任何部门)、也不含已退役的 ``cd`` / ``ai-team``
#: (见 ``dept_groups.RETIRED_GROUP_IDS``)。
_HAS_OWN_IMAGE = frozenset(
    {
        "product-system",
        "ivc",
        "radio-design-1",
        "radio-design-2",
        "radio-design-3",
        "radio-sw",
        "qa",
        "pmo",
    }
)


def category_image(category_id: str) -> str:
    """某个部门(category)的默认封面图 —— 前端静态资源路径,不是图片字节。

    文件在前端仓的 ``ai-portal-frontend/public/skill-images/``,随 ng build 烤进前端镜像。
    作者没有传自己的图时,卡片显示的就是这一张。

    这个函数原来叫 ``routers/upload._category_image``,只在上传时调过一次。搬到这里是因为
    现在有第二个调用点:编辑时换了部门,默认图要跟着换(见 routers/skills.update_skill)——
    原来不换,于是技能挪了部门之后卡片上还挂着原部门的图。
    """
    slug = category_id or "product-system"
    # 'public' 组没有专属图,借用 tavily 那张。
    if slug == "public":
        return "skill-images/tavily.jpg"
    # 没有专属图的组用共享兜底图。这里从前是逐个列特例(只列了 'ai-team'),改成按\
    # ``_HAS_OWN_IMAGE`` 判断是因为特例清单漏了两个真实分组:``others`` 与 ``cd`` 都没有\
    # 对应文件,于是它们的卡片一直指向一个 404 —— 而封面图坏掉不报错、只是显示破图,所以\
    # 没人报过。按"有没有这张图"来判断,新增分组时漏加图片的后果是退回兜底图而不是破图。
    if slug not in _HAS_OWN_IMAGE:
        return "skill-images/default.png"
    return f"skill-images/{slug}.jpg"


def normalize(raw: bytes) -> tuple[bytes, str]:
    """把上传的字节变成"我们生成的一张图"。返回 (字节, MIME)。

    不合格就抛 ``InvalidImage``。判定类型只看**解码结果**,不看文件名、也不看客户端声明的
    Content-Type —— 那两样都是调用方随便填的。
    """
    if not raw:
        raise InvalidImage("没有收到文件内容")

    # 第一遍:只做结构校验。verify() 会消耗掉这个对象,所以下面必须重新 open。
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            fmt = (probe.format or "").upper()
            probe.verify()
    except Image.DecompressionBombError as exc:
        raise InvalidImage("图片像素数过大,拒绝解码") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        # Pillow 对损坏文件抛的异常类型不统一,所以这里按一组接。
        raise InvalidImage("这个文件不是能识别的图片") from exc

    if fmt not in ALLOWED_FORMATS:
        raise InvalidImage(
            f"只支持 PNG / JPEG / WebP,收到的是 {fmt or '未知格式'}"
        )

    # 第二遍:真正解码、限尺寸、重新编码。
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img = img.convert(_SAVE_MODE[fmt])
            # thumbnail 是就地等比缩,且**不会放大**小图。
            img.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
            out = io.BytesIO()
            # 不带 exif=/icc_profile= 参数就等于把它们丢掉,这是想要的。
            img.save(out, format=fmt, optimize=True)
    except Image.DecompressionBombError as exc:
        raise InvalidImage("图片像素数过大,拒绝解码") from exc
    except (OSError, ValueError) as exc:
        raise InvalidImage("图片解码失败,可能已损坏") from exc

    data = out.getvalue()
    if not data:
        raise InvalidImage("图片解码后为空")
    # 重新编码后**变大**是可能的(比如一张高压缩比的 JPEG 存成 PNG 那种极端情况这里不会发生,
    # 因为格式不变;但 optimize 偶尔也救不回来)。既然入口已经按 MAX_UPLOAD_BYTES 卡过,这里
    # 再兜一道,免得库里出现比闸门更大的行。
    if len(data) > MAX_UPLOAD_BYTES:
        raise InvalidImage(
            f"图片处理后仍超过 {MAX_UPLOAD_BYTES // 1024} KB,请先压缩或裁小一些"
        )
    return data, ALLOWED_FORMATS[fmt]
