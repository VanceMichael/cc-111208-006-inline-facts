# supplierfacts

供应商结构化披露 XHTML 上下文与单位检查器。

## 本地运行

使用 Python 3.11 标准库，无 pip 依赖。所有来源与业务数据都是本地合成数据，不连接真实用户系统。

```sh
python -m scripts.materialize
python -m supplierfacts inspect fixtures/disclosure.xhtml
python -m compileall -q .
python -m unittest discover -s tests -v
python -m scripts.demo
```

检查命令输出单份来源的结构摘要。`capture URL --method GET --state var` 仅接受 loopback HTTP，保存原始响应为 SHA256 文件，并在 SQLite captures 表登记；可用 `--header 'Name:Value'` 传头、`--body FILE` 传正文，不跟随重定向。检查器不执行多页、业务去重。

## facts 接收流程

`facts SOURCE --state var/facts --origin 来源 --revision 修订` 把一份文档修订的完整 iXBRL 事实集合原子接收到 SQLite（`facts.sqlite3`），并在 `blobs/` 下按 SHA-256 留存原始页面：

- 命名空间感知解析 `contextRef`、`unitRef`、`decimals`、`scale`、`sign`、`xsi:nil` 与 `urn:local:ixt` 下注册的两种数字变换；QName 身份取命名空间 URI + 局部名，与前缀文本无关。
- 仅注册 `num-dot-decimal` 与 `num-comma-decimal`；未知变换、无法匹配的数字、非法属性以及悬空/循环/重复的续接链一律隔离并给出带节点定位的诊断，绝不猜测。
- 按概念、规范 context（主体、instant/duration、排序后的显式维度）与规范 unit（measure 排序、divide 分子/分母）分组；等值合并但保留全部来源节点，不等值形成冲突组，后出现节点不会覆盖先前节点。
- 报告每个来源节点的原值、精确 Decimal 变换结果、精度声明（decimals/INF 不改写金额）与节点定位。
- 解析全部在内存完成，事实集合在单事务内可见；解析失败或中途崩溃不留半份披露，同一文件重复接收不新增事实。

```sh
python -m supplierfacts facts fixtures/synthetic-disclosure.xhtml \
  --state var/facts --origin supplier-A --revision rev-1
```

新增能力与摘要工具的边界见 docs/acceptance.md，支持子集与失败语义见 docs/source-contract.md。本项目不联网查企业、不做风险评分，也不提供报表页面。

## 可复现容器

```sh
docker build -t supplierfacts:local .
docker run --rm --network none supplierfacts:local python -m unittest discover -s tests -v
docker run --rm --network none supplierfacts:local
```

镜像固定基础摘要与 facts 接收流程。构建在正式运行前完成；运行和测试不安装包、不下载数据。演示命令实际执行检查器、本地来源与一次 facts 业务闭环（含冲突组、隔离项与重复接收幂等），通常不超过 5 秒。

## 协议与来源

- docs/source-contract.md 给出支持子集、字段及失败语义。
- docs/acceptance.md 给出业务验收边界。
- fixtures/source.json 是本地可重放 HTTP 路由；`python -m scripts.source_server --port 8765` 启动，`/__requests` 可检查实际请求。
- scripts/materialize.py 将版本化 base64 二进制夹具还原为原始字节，重复执行安全；二进制文件不是用户附件。
- journal.py 只记录原始响应；facts 接收流程在 receive.py 中使用独立的 SQLite 事务、内容摘要去重与崩溃恢复设计。
