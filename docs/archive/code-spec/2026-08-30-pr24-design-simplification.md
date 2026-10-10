# PR #24 新增代码设计简化 Spec（修订版）

日期：2026-08-30
范围：`fix/agent-output-limits-and-shell-encoding` / `feat(serving)` 新增代码
状态：已反思修订

---

## 0. 反思：上一版方案的缺点

上一版计划把 `predictions_api.py` 拆成 `config.py / features.py / predictor.py / http_api.py` 四个模块，并引入 `ModelConfig`。反思后认为：

1. **Module 数量不是心智负担的核心**：四个文件反而增加 import、模块边界和概念数量。
2. **`ModelConfig` 会让 attrs 更多**：它要承载 threshold、feature_columns、required_fields、nullable_fields、medians、thresholds、versions、examples 等十几个字段，反而违背“减少 class attrs”。
3. **`Predictor` 基本是 `ModelBundle` 换名**：没有减少方法数，只增加了一层包装。
4. **真正的问题是 `ModelBundle` 同时保存了原始 `preproc` dict 和展开后的重复字段**，以及 `validate()` 返回裸元组。
5. 最合理的简化是：**保留单文件或最多两文件，减少对象属性，不新增大型配置类**。

---

## 1. 简化目标

- 减少 `ModelBundle` 的存储属性。
- 把 `validate()` 的裸元组换成语义化值对象。
- 保持 HTTP 层薄。
- 不新增无意义抽象，不增加模块数量。
- 严格保留所有行为边界。

---

## 2. 修订后的结构（两文件）

```text
src/athena/serving/
├── model.py       # 常量、create_features、Booster、ValidationError、PredictionRequest、ModelBundle
└── http_api.py    # Handler、build_server、main
```

不引入：

- `ModelConfig`
- `Predictor` 类
- `config.py`
- `features.py`

---

## 3. `model.py`

### 3.1 `ModelBundle` 精简

当前存储属性：

```python
booster
preproc
source
default_threshold
required_fields
nullable_fields
```

修订后只保留：

```python
booster
preproc
source
```

其余改为 property：

```python
@property
def default_threshold(self) -> float:
    return float(self.preproc["threshold"])

@property
def required_fields(self) -> tuple[str, ...]:
    return tuple(self.preproc.get("required_fields") or DEFAULT_REQUIRED_FIELDS)

@property
def nullable_fields(self) -> tuple[str, ...]:
    return tuple(self.preproc.get("nullable_fields") or DEFAULT_NULLABLE_FIELDS)
```

效果：

- class attrs 从 6 降到 3。
- 不会出现“原始 dict 与展开字段同时维护”的同步问题。
- `preproc` 仍是唯一配置来源。

### 3.2 新增 `PredictionRequest`

```python
@dataclass(frozen=True, slots=True)
class PredictionRequest:
    records: list[dict]
    threshold: float
```

`ModelBundle.validate()` 改为：

```python
def validate(self, payload: object) -> PredictionRequest:
    ...
```

调用方不再需要记住“第一个是 records，第二个是 threshold”。

### 3.3 `create_features`

原样保留在 `model.py` 中，不单独拆文件。

保留边界：

- 必须与训练脚本同名函数逐字一致；
- 这是推理独立部署的硬边界。

---

## 4. `http_api.py`

### 4.1 Handler 保持薄

Handler 只做：

- 解析路由；
- 读取/校验 HTTP 请求；
- 调用 `ModelBundle.validate()` / `predict()`；
- 写 JSON 响应。

不再直接访问：

```python
bundle.preproc["feature_columns"]
bundle.preproc.get("threshold_rule", "")
```

这些通过 `ModelBundle` 的少量只读方法或 property 提供，例如：

```python
def predict_response(self, threshold: float, predictions: list[dict]) -> dict: ...
```

如果不想新增方法，至少保留 `schema()/describe()/example()` 作为 metadata 出口，HTTP 层不踩原始 dict。

### 4.2 server 构造

保留当前 `build_server(bundle, host, port)` 即可。

可选小改进：

```python
def build_server(bundle: ModelBundle, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"bundle": bundle})
    return ThreadingHTTPServer((host, port), handler)
```

这个动态 type 写法可保留，因为它把不同 bundle 隔离到不同 handler 类，避免类级共享状态。
如果追求更直白，也可以：

```python
def build_server(bundle, host, port):
    Handler.bundle = bundle
    return ThreadingHTTPServer((host, port), Handler)
```

但后者会改变类级状态，不如前者安全。**本修订不强制改这里**。

---

## 5. 接口简化效果

| 当前 | 修订后 |
|---|---|
| `ModelBundle` 6 个存储 attr | 3 个存储 attr + 3 个 property |
| `validate()` 返回 `(list, float \| None)` | 返回 `PredictionRequest` |
| 464 行单体 | 两文件：`model.py` + `http_api.py` |
| HTTP 层访问原始 `preproc` dict | HTTP 层主要走 `schema()/describe()/example()/validate()/predict()` |
| 无新增大型配置类 | **不新增** `ModelConfig` / `Predictor` |

---

## 6. 保留边界（验收）

1. 所有 HTTP 路由、状态码、CORS 不变。
2. 三种请求形状不变。
3. required / nullable 字段规则与错误文案不变。
4. threshold 默认值与覆盖规则不变。
5. 缺值插补与 missing flag 不变。
6. `create_features` 输出列名与训练脚本一致。
7. `lightgbm` 仍惰性导入，未安装时模块可导入。
8. 不执行 shell、不写盘、请求间无状态。
9. 默认绑定 127.0.0.1，公网绑定仍告警。
10. `/example` 示例与真值不变。

---

## 7. 实施步骤

1. 在 `serving/model.py` 建立 `ModelBundle` 精简版 + `PredictionRequest`。
2. 在 `serving/http_api.py` 建立 Handler / build_server / main。
3. 删除或改为薄入口的旧 `predictions_api.py`。
4. 更新测试 import，不改测试语义。
5. 运行 `test/unit/serving/test_predictions_api.py` 全部通过。

---

## 8. 不做的简化

- 不新增 `ModelConfig` / `Predictor`。
- 不拆成四个模块。
- 不消除 `create_features` 与训练脚本的复制关系。
- 不引入 Web 框架。
- 不改变 serving 与 GUI Gateway 的隔离边界。
