# Prompt & LLM Test Tool

一个用于对比测试不同大模型 × 不同 Prompt 组合在主观题批改任务上表现的 Web 工具。

支持 OpenAI 兼容格式的所有模型 API（通义千问、DeepSeek、豆包/火山引擎、OpenAI 等），通过可视化界面完成实验配置、并发运行、结果分析和数据导出的全流程。

## 快速开始

### 1. 启动服务

需要 Python 3（无第三方依赖）：

```bash
python3 server.py
```

默认在 `http://localhost:8080` 启动。可指定端口：

```bash
python3 server.py 9090
```

打开浏览器访问即可使用。

### 2. 加载题目数据

进入「数据管理」标签页，上传 CSV 文件。CSV 需要包含以下列：

| 列名 | 说明 |
|---|---|
| `question` | 题目内容 |
| `user_answer` / `user_final_answer` | 学生答案 |
| `final_right_answer` / `right_answer` | 标准答案 |
| `is_right` | 标注（TRUE/FALSE），作为评判模型批改准确性的 ground truth |
| `question_type` | 题型（选择题/填空题/解答题/计算题/综合题） |

### 3. 配置模型

进入「实验配置」标签页，可以：

- **快速添加**：点击模型模板（预填了 API 地址和模型 ID），只需输入 API Key
- **手动添加**：自定义模型名称、服务商、API 地址、模型 ID

#### 多 API Key 轮换（推荐）

每个模型最多支持 3 个 API Key。系统会将题目均匀分配到不同 Key 上并行请求，有效突破单 Key 的限流瓶颈。

**为什么要填多个 Key？** 大模型 API 对每个 Key 都有请求频率限制（QPM）。同一个 Provider 的多个模型如果共享一个 Key，会互相抢占额度。给每个模型配独立 Key（甚至多个 Key），可以让请求速率翻倍。

#### 各平台 API Key 获取

| 平台 | 获取地址 | 注意事项 |
|---|---|---|
| 通义千问 (DashScope) | https://dashscope.console.aliyun.com/ | 创建 API Key 即可 |
| DeepSeek | https://platform.deepseek.com/ | 创建 API Key 即可 |
| 豆包 (火山引擎) | https://console.volcengine.com/ark | 需创建推理接入点，模型 ID 填接入点 ID（如 `ep-xxxx`） |

### 4. 配置 Prompt

在「实验配置」中添加或上传 Prompt 文件。每个 Prompt 需指定类型：

- **judge**：判题型，用于选择题/填空题（输出对/错）
- **subjective**：主观题批改型，用于解答题/计算题/综合题（输出分步分析）

Prompt 中支持以下占位符（会自动替换为题目数据）：

- `{{QUESTION_CONTENT}}` / `{question}` → 题目内容
- `{{STUDENT_ANSWER}}` / `{user_answer}` → 学生答案
- `{{STANDARD_ANSWER}}` / `{final_right_answer}` → 标准答案
- `{{SUBJECT}}` / `{subject}` → 学科

如果不使用占位符，题目数据会自动拼接在 Prompt 末尾。

### 5. 配置题型映射

在「实验配置」底部，设置每种题型使用哪类 Prompt 进行评测。默认映射：

- 选择题 / 填空题 → judge
- 解答题 / 计算题 / 综合题 → subjective

### 6. 运行实验

进入「运行实验」标签页：

1. 在组合矩阵中勾选要测试的「模型 × Prompt」组合（可点全选/全不选）
2. 设置运行参数：
   - **最大并发数**：同时运行的请求数（建议 4-8，取决于你的 Key 数量）
   - **请求延迟**：每个请求之间的间隔（ms），建议 500
   - **单次超时**：单次 API 调用的最长等待时间（ms），建议 60000
   - **失败重试次数**：API 调用失败后的重试次数，建议 1
3. 点击「开始运行」

运行过程中可以实时查看进度，也可以随时点击「停止」。

### 7. 查看结果

进入「结果分析」标签页：

- **统计概览**：每个「模型 × Prompt」组合的准确率
- **详细结果表**：可按 Prompt 筛选，横向列出模型，纵向列出题目，点击单元格可查看模型原始响应
- **按模型/Prompt/题型统计**：各维度的准确率柱状图

### 8. 导出数据

- **导出结果**：CSV 格式，包含每个组合每道题的判题结果和错误标签
- **导出总结表**：精简版汇总表，每个模型一列
- **导出日志**：JSON 格式的完整运行日志

## 架构说明

```
评测工具/
├── server.py        # Python 后端（HTTP 服务器 + API 代理 + 限流）
├── index.html       # 前端单页应用
├── .gitignore
├── README.md
└── data/            # 运行时生成，不入版本控制
    ├── config.json  # 模型/Prompt 配置（含 API Key）
    └── results.json # 实验结果
```

### 为什么需要 Python 后端？

浏览器有 CORS（跨域安全策略）限制，无法直接从前端调用大模型 API。Python 后端作为代理转发请求，同时提供：

- **API 代理**：转发前端请求到各模型 API，绕过 CORS
- **令牌桶限流**：按 Provider 自动分组限速（默认 5 req/s），避免触发 API 限流
- **429 处理**：上游返回限流响应时，传递 `Retry-After` 信息给前端
- **数据持久化**：配置和结果保存到本地文件，刷新不丢失

### 前端限流机制

前端 `callModelAPI` 检测到 429 响应后，会自动指数退避重试（2s → 4s → 8s → 16s → 30s），最多重试 5 次。这确保了即使后端令牌桶未能完全避免限流，请求也不会直接失败。

## 注意事项

1. **API Key 安全**：API Key 存储在本地 `data/config.json` 中，不会上传到任何服务器。但请注意不要将 `data/` 目录提交到公开仓库。
2. **费用**：本工具会真实调用模型 API，每次实验都会产生 API 调用费用。请注意控制实验规模。
3. **并发设置**：并发数不是越高越好。建议设为「API Key 总数 × 2」，例如 3 个 Key 配 6 并发。
4. **超时设置**：主观题批改的 Prompt 通常较长，模型推理时间也长。建议超时设为 60-120 秒。

## 许可

MIT
