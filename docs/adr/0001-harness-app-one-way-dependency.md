# harness 与 app 单向依赖，且 harness 不发 pip 包

EvaAI 分为 harness（框架层）与 app（业务层）两个子包，依赖方向单向：app 可以 import `evaharness`，反向禁止，由 `tests/test_harness_boundary.py` 卡死。这样框架层保持业务无关、可独立测试，业务侧改动无法渗进框架。harness 是 backend 内的独立子包而非独立发布的 pip 包——本地个人项目不值得维护发版流程，而边界由测试保证，不靠仓库隔离。

## 考虑过的选项

- **单包内用目录分层**：边界只靠自觉，业务代码很容易慢慢长进框架层。
- **拆成独立仓库 / 发 pip 包**：边界最强，但对本地个人项目是过重的流程负担。
