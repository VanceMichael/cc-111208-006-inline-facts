# 供应商结构化披露子集

输入是格式正确的 XHTML，XBRL instance 命名空间为 http://www.xbrl.org/2003/instance，inline 命名空间为 http://www.xbrl.org/2013/inlineXBRL。QName 身份使用命名空间 URI 与局部名，不是前缀文本。context 含主体 scheme/value、instant 或 duration，以及 xbrldi:explicitMember 显式维度；展开维度名和成员名后排序。unit 支持 measure 与 divide 中的分子/分母，各自排序规范化。

仅注册 urn:local:ixt 下 num-dot-decimal（1,234.50）和 num-comma-decimal（1.234,50）两种变换；括号表示负数，scale、sign 各应用一次，精确 Decimal 计算，decimals 或 INF 独立保留为精度声明。nil 按 xsi:nil 处理。未知变换隔离，不能猜测。非数值事实按 continuedAt 续接，排除 ix:exclude 子树；重复节点 ID、循环及悬空续接须定位节点。

按概念、规范 context、规范 unit 分组；等值合并但保留全部来源节点，不等值形成冲突组。文档身份绑定内容摘要与来源修订，整份事实集合原子可见，不留下半份披露。样例包含缩放负值、冲突金额和跨节点文本；当前检查器只列 context/unit，不提取事实。不得解析外部实体或联网加载资源，不包含企业风险评分。
