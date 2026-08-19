# 技能仓怎么访问

事实与投影的划分见 [security-compliance.md](security-compliance.md)。这一份只讲一件事:后端**怎么**读写
那个 git 仓库。

## 现在的样子

一棵共享的本地工作副本(`settings.skill_repo_path`,bind mount),五个使用者:

| 谁 | 干什么 |
|---|---|
| `skill_storage.store()` | 写:切分支、写文件、`add`、`commit`、`tag`、`push` |
| `skills.py` `_read_skill_md` | 读 SKILL.md(编辑与审计的输入) |
| `skills.py` 下载接口 | 读整个技能目录打 zip 给用户 |
| `official_import` | 读 `official/`,但为了读到最新会 `fetch` + `merge --ff-only`,失败则 `checkout origin/main -- official/` |
| `export_skills.py` | 一次性命令行工具,写 + 提交 + 推送 |

## 问题不是"缺事务",是把一棵共享可变工作树当成了 API

**最严重的一条:审计闸门可以被绕过。** 可疑内容走 `checkout -B <复核分支>`;在这个窗口里,另一个并发的
正常发布调用 `push HEAD:main` —— 而此时 HEAD 在复核分支上,**未经复核的内容就上了 live**。

其余四条:

* `add -A -- <路径>` 有范围,紧随其后的 `commit -m` 没有 pathspec,提交的是整个索引 —— 别人刚 stage
  的文件会被卷进这次提交。
* `checkout -f main` 会丢掉另一个请求刚写下、还没提交的文件;`official_import` 的 `merge --ff-only`
  会在别人写到一半时挪动 HEAD。
* **push 失败不抛异常**,只返回 `pushed=False`,数据库照常提交 → 市场里看得到、从 GitLab 下载 404。
  这是最可能发生的一条,取决于网络。
* git 提交成功、数据库事务失败 → 仓库里有内容、数据库没有行,而**收敛器只遍历数据库的行,永远补不回来**。

另外版本号来源是反的:从**数据库**读上一版加一,然后写进 manifest、打 tag。两个并发发布会算出同一个
版本号,而 tag 冲突时代码是默默跳过 —— 两份不同内容共用一个版本号,没人知道。

## 抽象

**技能仓只通过 GitLab 访问;本地的任何副本都只是可以随时丢掉的缓存,按 commit sha 寻址。**

这句话同时管住读和写,并且解释了为什么允许有本地缓存:缓存能重算,所以它是投影,不是事实 —— 与
数据库同一条规矩。它**删除**了"两个进程抢一棵工作树"这个问题,而不是用锁去管理它。

## 写

一次原子提交(`POST /projects/:id/repository/commits`),一个技能这一版的全部文件放在同一批 `actions`
里,该删的旧附件也放同一批。

* 每个**已存在**的文件带上 `last_commit_id`。不带就是静默覆盖。
* **冲突返回 409 给调用方,不重试。** 因为每个技能的文件路径是自己的,`last_commit_id` 冲突只可能
  意味着同一个技能被别人同时改了;重试会用自己这份把对方的修改覆盖掉。只有网络错误和 5xx 才重试。
* **内容哈希做幂等**:写之前比对 manifest 里记的哈希,一样就跳过并返回现有版本与 sha。接口本身不幂等
  (相同内容也会产生一个空提交),没有这道闸,客户端重试一次就多涨一个版本号。这道哈希同时满足审计
  文档要求的"内容指纹",能证明上架的就是被审计的那份。
* 可疑内容:建复核分支 + 首次提交是同一个请求(`start_branch`),然后开 Draft MR。分支名带审计 ref,
  所以不会互相撞。**没有本地工作树,所以上面那个闸门绕过的洞是被结构消除的,不是靠加锁躲开的。**
* 版本号从**仓库**里那份 manifest 推,不从数据库加一。

## 读

单文件 `files/:path/raw`、列目录 `repository/tree`、子目录归档 `repository/archive.tar.gz?path=`,
全部带 `sha`。

* 缓存键是(路径, commit sha)。放**容器本地**目录 —— 不能是 bind mount,否则又把副本钉回一台机器。
  要有大小上限,并记命中率:没有命中率数据,缓存悄悄失效了没人会发现。
* **读失败不要回落到数据库里的 `instructions`。** 那是第二份事实,回落等于拿旧内容去审计或编辑。

## 数据库是投影

* 顺序固定:**先提交 GitLab,再写数据库**。反过来永不做 —— "数据库有、仓库没有"无法从事实恢复。
* 收敛器加一步:列出 **live 分支**上的 manifest,数据库缺行就补、版本落后就更新。只认 live 分支,
  复核分支上的内容按定义还没上线。
* 字段分工**复用 `official_import` 已有的那套**,不要另立一套:manifest 拥有的字段照抄,运行时字段
  (`rating` / `downloads` / `review_count` / `is_trending`)保留不动。

## 实测过的接口行为

在真实仓库上验的(探测分支已删,`main` 未被触碰):

| 试的什么 | 结果 |
|---|---|
| 一次提交多个文件 + 二进制 | 成立,1MB 回读字节一致 |
| 体积 | 30MB 原始文件(请求体 40MB)也过,12 秒。当前最大附件 9.3MB |
| 一次提交里混 create + delete | 成立 |
| `update` 带正确 `last_commit_id` | 201 |
| `update` 带过期 `last_commit_id` | **400 `The file has changed since you started editing it`** |
| `update` 不带 `last_commit_id` | 201,**静默覆盖** |
| `start_sha` | 只用于建分支;分支已存在时报 "branch already exists"。**不是并发保护** |
| 内容完全相同的 `update` | 201,**产生一个空提交**(所以接口不幂等) |
| `create` 已存在 / `update` 不存在 | 各自 400,所以要先知道文件在不在 |
| 子目录归档(9.1MB) | 2.1 秒 |
| 列目录 / 单文件 raw | 各 0.5 秒 |

## 放弃了什么

* **可移植性**。这套绑在 GitLab 的接口上。GitOps 工具(如 Flux 的镜像自动更新)坚持 clone+push 就是
  为了支持任意 git 服务器。我们长期在 GitLab 上,所以接受这个绑定 —— 但这是这个选择唯一实质的代价。
* **读也依赖 GitLab 可达**。今天 GitLab 挂了还能本地提交、稍后再推,但那正是 `pushed=False` 静默分叉
  的来源。这是拿"悄悄不一致"换"明确失败"。
* **下载首次变慢**(最大的技能 2.1 秒),缓存之后只有第一次。

## 两件待定

1. **删除语义**:技能目录从仓库消失时,数据库那行要不要跟着消失?`official_import` 现在写的是"从不
   删除"。如果数据库是投影,这是个口子。选一个写下来。
2. **token 级别**:`main` 是保护分支,实测只允许 **Maintainer(access_level 40)** 推送,没有
   Developer。所以专用 token 必须是 Maintainer,**不能是 Developer**;或者所有落 main 的改动都走 MR。
   现在用的 token 是那个仓的 Owner,权限过大。

## 落地顺序

每步都能单独回退。

0. **两行止血,不等重构**:`commit -m msg -- <路径>` 加上 pathspec;push 用明确分支名
   (`refs/heads/main:main` / `<复核分支>:<复核分支>`)而不是 `HEAD:`。今天就消除闸门绕过。
1. **先切读**:下载、`_read_skill_md`、`official_import` 改走接口 + 缓存。风险最低,先把"本地只是缓存"
   这条规矩立起来。
2. **再切写**:发布器取代 `skill_storage`。
3. **删干净**:两个挂载(技能仓、ssh 私钥)、`skill_repo_*` 配置、`SkillStorage`。
4. 收敛器加 manifest 补齐。

做完第 3 步,`audit-backend` 就没有本地状态了,可以在任意一台机器上起来 —— 这才是这次重构的目的,
不是抗并发。发布器**是模块不是独立服务**:一天两次上传,独立服务只在需要串行化或独立扩容时才划得来,
而这次改完连串行化都不需要了。
