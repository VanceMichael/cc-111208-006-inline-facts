# 接收流程核验

facts 接收流程已实现（`supplierfacts facts`、`supplierfacts/receive.py`），与原文留存、单份检查并存；端到端回归测试在 `tests/test_facts_flow.py`，原有 `tests/test_baseline.py` 只证明原功能。

- 改 QName 前缀和维度顺序不改变事实身份（test_dimension_reorder_and_prefix_swap_preserve_identity）
- 精确金额、括号负值、缩放、nil、未知变换与精度各自保留（ExtractionTests 组）
- 续接链断裂/循环有节点定位；冲突组与重复文档接收原子化（test_cycle_continuation_is_located、ReceiverTests、CrashAtomicityTests）
- 解析失败或写入中途崩溃不留半份对外可见披露；崩溃测试启动真实子进程并以非零状态终止

允许扩充 fixtures/source.json。运行本地服务后可访问 /__requests 核对真实方法、请求头和正文。每次演示用新的临时状态目录；崩溃测试必须启动子进程并实际终止，不只模拟抛异常。正式演示选择上述业务闭环中的一个，控制在 90 秒内；准备依赖和构建在演示前完成。当前 `python -m scripts.demo` 完成 capture→facts→冲突/隔离→幂等重复接收闭环，耗时约 1 秒。
