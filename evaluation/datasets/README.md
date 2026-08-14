# Evaluation Datasets

这些 JSON 文件是给后续 evaluation runner 和同事手动对照用的标准假数据。

当前包含：

```text
adaptive_loop_cases.json
summary_cases.json
memory_cases.json
safety_cases.json
```

它们不是运行时代码，不要求完全等同 Pydantic schema；它们描述输入场景和期望结果。
