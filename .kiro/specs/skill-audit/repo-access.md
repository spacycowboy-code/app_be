# 技能仓怎么访问

事实与投影的划分见 [security-compliance.md](security-compliance.md)。这一份只讲后端怎么读写技能内容。

## 抽象

**门户不持有可变的内容副本。内容只通过不可变引用读取;发布是一次带前置条件的原子写,返回新的引用。**

git 是这个内容存储今天的实现,同时承担复核工作流;引用今天是 commit sha。厂商不进抽象——否则接口会带上
`project id` / `ref` / `last_commit_id`,渗到调用方,换实现就变成重写。

```
read(ref, path)             -> bytes          # read_text 是它的文本包装
list_files(ref, prefix)     -> [path]
read_tree(ref, prefix)      -> {相对路径: 字节}
head_ref(branch)            -> ref            # 默认分支现在是哪一版
version_of(prefix, at=ref)  -> ref            # 这个子树的内容身份
publish(files, expect=ref)  -> ref            # expect 不匹配 -> 冲突
```

`last_commit_id` 留在实现里面,是"比较并交换"的前置条件。

**`read_tree` 而不是"返回归档字节"**:归档带两层前缀(GitLab 的 `<项目>-<ref>-<ref>-<路径>/` 包装目录,
以及技能在仓库里的完整路径),剥壳是存储细节。交出路径到字节的映射,调用方自己决定打包格式。
格式用 `archive.zip` 而不是 `tar.gz`:门户已经在用前者(市场下载),两种格式并存没有理由。

**`version_of` 返回的是内容身份,不是"某个提交"**:调用方要的是"只在这个技能自己变了的时候才变的标识"。
用整仓 head 当它,任何无关提交都会把每个技能标记成有更新。今天 git 用"最后碰过这个路径的提交"回答,
产物存储用包的 digest 回答 —— 所以它是不透明值,调用方不该解析。

**`publish` 的原子单位必须是"切换一个 manifest / 指针",不是"原子地写 N 个文件"。**
git 顺便给了 N 文件原子性,但对象存储没有跨对象事务(S3 的 `If-Match`、GCS 的 `ifGenerationMatch`
都是单对象的)。抽象依赖 git 这项额外能力,换实现就是重新设计而不是换实现。

今天的样子与此相反:一棵共享工作副本(`settings.skill_repo_path`,bind mount)上跑 git 命令,六处碰它
(`skill_storage` 写、`skills.py:55` 读 SKILL.md、`skills.py:316` 打 zip、`official_import` 移动整棵树的
HEAD、`export_skills.py`、`config.py:31`)。**目的是解开钉机器**:两个挂载(工作副本、ssh 私钥)不消失,
后端换机器就起不来,生产也上不了集群。抗并发是顺带的——一天两次上传不需要串行化。

## 真相按字段分

* **内容** → git 权威(需要历史、diff、复核)
* **门户事实** → 数据库权威(下载量、收藏、评价、审计记录、用户)
* **上架状态** → 投影,由 MR 状态推出

判据:能重算的是投影,不能的是事实。每个投影必须有一条真跑得起来的重算路径,否则会变成第二份事实。
`rp_ai_skill.instructions`(22 行非空)按这条是缓存,不是回落对象。

## 写

一次原子提交(`POST /projects/:id/repository/commits`),一个技能这一版的全部文件与要删的旧附件同一批。

* 已存在的文件必须带 `last_commit_id`。不带就是静默覆盖。
* **冲突返回 409,不重试。** 每个技能有自己的文件路径,冲突只可能是同一技能被同时改;重试会拿自己那份
  盖掉对方。只有网络错误和 5xx 才重试。
* **幂等靠内容哈希**:写前比对 manifest 里的哈希,一样就返回现有版本与 sha。接口本身不幂等(相同内容也
  产生空提交)。这道哈希同时是审计要求的内容指纹,能证明上架的就是被审计的那份。
* 可疑内容:建复核分支与首次提交是同一个请求(`start_branch`),然后开 Draft MR。分支名带审计 ref。
* 版本号从**仓库里的 manifest** 推,不从数据库加一。两个并发发布从数据库加一会算出同一个版本号,而 tag
  冲突时代码是默默跳过。

## 读

单文件 `files/:path/raw`、列目录 `repository/tree`、子目录归档 `repository/archive.tar.gz?path=`,都带 `sha`。

* 缓存键是(路径, sha)。放**容器本地**目录,不能是 bind mount,否则又把副本钉回一台机器。要有大小上限,
  要记命中率——没有命中率数据,缓存悄悄失效了没人会发现。
* **读失败不回落到 `instructions`。** 那是第二份事实,回落等于拿旧内容去审计或编辑。
* 对外仍然给 zip,契约不变。
* **超时预算按 11 秒设**:GitLab 按需生成归档,最大那个技能首次 10.84 秒、紧接着再取 2.08 秒。

## 产物存储:现在不做

注册中心(npm / PyPI / crates.io)在发布时把不可变产物放进对象存储,下载不经过 VCS;Backstage 把 git 摄取
进自己的库,官方文档称自己的目录为 caching mechanism。这么做主要是为读流量的规模和可用性解耦,不是为
不可变——git 本身内容寻址,sha 就是 digest,27 个版本的 sha 已全部记录。

**当前读量每天 9 次下载**(87 条记录 / 最近 7 天 62 条),按 sha 读 + 缓存是够用方案,不是过渡方案。
满足以下任一条再做,做的时候是换实现而非重写:

* 下载必须在 GitLab 不可用时仍然可用
* 读量涨到 GitLab 开始限流
* 已发布产物需要活得比仓库长(现在的规矩是仓库删了置 `archived`,不删行)

做的话要先解决三件不在本仓控制范围内的事:Artifactory 通用仓库的权限、合入触发摄取用 webhook 还是 CI、
以及"MR 合了但产物没进去算不算已发布"——那又是一次双写。

## 数据库是投影

* 顺序固定:**先提交 git,再写数据库**。反过来那种失败("数据库有、仓库没有")无法从事实恢复。
* "先 git 后数据库"就是双写,两次写不可能原子。所以数据库那行必须能由 git 重放出来:收敛器列出 live
  分支上的 manifest,缺行就补、版本落后就更新。**收敛器是重放路径,不是补丁**,而且必须能对任意 commit
  重放,不只是刚刚那一次。
* 字段分工复用 `official_import` 已有的那套:manifest 拥有的字段照抄,运行时字段(`rating` /
  `downloads` / `review_count` / `is_trending`)不动。

## 放在哪一层

新模块是仓储层,放 `app/repositories/`,和 dao 同一个意思。将来把数据库查询也挪进这一层是同一个模式,
不是第二套约定。

现状:`app/routers/` 同时干 HTTP 和数据访问(10 处直接查库、2 处直接读文件),`app/services/` 是领域操作,
`app/models/` 只有表定义,**没有 dao 层**。所以"内容访问"这个边界今天不存在,建它必然动路由层。

数据库查询散在路由里单独处理,不和这次混在一个 diff 里:它只影响整洁,不产生错误结果。

## 删除语义

**仓库里没了 → 收敛器把 `status` 置成 `archived`,不删数据库行。**

那一行带着 git 里算不出来的门户事实。为镜像一次仓库删除去销毁它们,是把"数据库是投影"用反方向——
投影指派生字段能重算,不是门户自己的事实可以丢。

公开列表按 `status == "approved"` 过滤(`skills.py:154`),置 `archived` 后自然消失,前端不用改。
写入者仍然只有收敛器。

## token

用项目访问令牌(Maintainer),不用个人令牌。`main` 是保护分支,实测只允许 access_level 40 推送,
Developer 会被拒,所以"专用 Developer token"走不通。今天服务用的是一个同事的个人 token,后果是服务开的
每个 MR 都显示成她,而且她轮换或离开服务就断。

引擎只读技能仓,用单独的只读令牌:`skill-audit-engine-read`(测试)、`skill-audit-engine-read-prod`
(生产),scope 只有 `read_api` + `read_repository`,Reporter,到期 2027-08-19。

## 实测过的接口行为

真实仓库上验的,探测分支已删、`main` 未触碰。

| 试的什么 | 结果 |
|---|---|
| 一次提交多文件 + 二进制 | 成立,1MB 回读一致 |
| 30MB 原始文件(请求体 40MB) | 成立,12 秒 |
| 一次提交里混 create + delete | 成立 |
| `update` 带正确 `last_commit_id` | 201 |
| `update` 带过期 `last_commit_id` | **400 `The file has changed since you started editing it`** |
| `last_commit_id` 到底跟什么比 | **按文件比,不按分支比**:问的是"这个文件在这个提交之后被改过吗"。文件自己的最后提交、当前分支头都成立;无关提交把分支头推走**不会**造成冲突(三次对照实测)。所以一个 `expect` 引用能覆盖这次要写的所有文件,不必逐文件查最后提交 —— 而"冲突只可能是同一个技能被并发改"这条推理成立,409 不重试是对的 |
| `update` 不带 `last_commit_id` | 201,**静默覆盖** |
| 内容完全相同的 `update` | 201,**产生空提交**(接口不幂等) |
| `start_sha` | 只用于建分支;分支已存在报 "branch already exists"。**不是并发保护** |
| `create` 已存在 / `update` 不存在 | 各自 400,所以要先知道文件在不在 |
| 列目录 / 单文件 raw | 各 0.5 秒 |

归档取回与转 zip,22 个技能全量测(2026-08-20,22/22 成功):

| | 结果 |
|---|---|
| 最大技能 | 9315K tar.gz → 9316K zip,3 个文件 |
| 取回 | 中位 0.8 秒,平均 1.64 秒,**最慢 10.84 秒**;同一归档再取 **2.08 秒** |
| tar.gz → zip | 最慢 0.30 秒,峰值内存 32MB(后端限额 2G,实际用 187MiB) |

生产 22 个技能的 `storage_path` 与 `repo_commit` **全部有值**,`rp_ai_skill_version.commit_sha` 27/27
有值。所以删掉"回落到 `instructions`"不需要数据迁移;首次下载会从本地磁盘的近乎瞬时变成 0.6–11 秒。

## 代价

* 绑定 GitLab 接口。GitOps 工具坚持 clone+push 是为了支持任意 git 服务器;我们长期在 GitLab 上,接受。
* 读也依赖 GitLab 可达。今天 GitLab 挂了还能本地提交稍后再推,但那正是 `pushed=False` 静默分叉的来源。
  这是拿"悄悄不一致"换"明确失败"。

## 顺序

每步能单独回退。**动手时机:等重复性维度阶段性验收之后**——会改到路由与服务两层,和正在进行的功能开发
落在同一批文件上。

0. ~~止血:`commit` 加 pathspec、push 写明分支名~~ **已完成**(`skill_storage.py:242`、`:115`)
1. ~~**切读**:下载、`_read_skill_md`、`official_import` 改走接口 + 缓存~~ **已完成**
   (!48 / !49 / !50)。`app/routers/skills.py` 里已无 `skill_repo_path`;`official_import` 不再移动
   共享树的 HEAD。三处原来都没有测试覆盖,现补 22 条并逐条用 mutation 验证过会失败。
2. **切写**:发布器取代 `skill_storage`。`publish` 已落地并在真仓库上验过(新目录+切分支、相同内容不产生提交、改删增一次原子提交、过期引用报冲突、`files={}` 清空目录;探测分支已删)。剩下的是把 `store()` 接过来
3. **删干净**:两个挂载、`skill_repo_*` 配置、`SkillStorage`
4. 收敛器加 manifest 补齐

做完第 3 步 `audit-backend` 就没有本地状态,可以在任意机器上起来。发布器是模块不是独立服务:独立服务只在
需要串行化或独立扩容时才划得来,而改完连串行化都不需要。
