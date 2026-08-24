# Brain × sealos-skills-next 适配

状态：**已实施**（本仓库侧全部落地；待 Brain 环境实跑验收）。
日期：2026-08-20。
范围：Brain GitHub deploy（Devbox + Agent）如何驱动本仓库的 skill 完成部署。

**硬约束：只能改 sealos-skills-next，Brain 一行代码不动。** 因此 Brain 里所有
硬编码都是本仓库必须满足的合同，下面逐条列出（全部经 Brain 源码核实）。

---

## 1. 结论先行

Brain 的安装器和 prompt 是不可协商的（`runner.ts`、`gateway-prompt.ts`）：

- 安装命令：`timeout 180 npx --yes skills@1.5.20 add "$DEPLOY_SKILL_SOURCE" -y`，
  之后**硬性检查**两个文件存在（`.agents/skills/` 或 `.codex/skills/` 下）：
  `sealos-deploy/SKILL.md` **和** `k8s-kaniko-job/SKILL.md`。缺一个整个任务失败。
- Prompt 硬编码 “run the sealos-deploy skill in managed mode” 和
  “run /sealos-deploy to completion”。

所以旧方案（§旧 8.1 “不拆 sibling、Brain 改安装器”）作废。新形态：

```
plugins/sealos/skills/
  use-sealos/        不变（本地交互路径原样），仅 Preflight 加一道 managed 门
  sealos-deploy/     新增：Brain managed 模式总入口（SKILL.md，无脚本，复用 use-sealos）
  k8s-kaniko-job/    新增：沙箱构建执行器（SKILL.md + scripts/kaniko-build.py）
skills/              三个同名 symlink（skills.sh / 本地路径安装入口）
```

`sealos-deploy` 和 `k8s-kaniko-job` 在非 managed 环境下是惰性技能（SKILL.md
明确让路给 use-sealos），对现有各宿主无行为影响。Brain 侧唯一需要动的是
**部署配置**（非代码）：`DEPLOY_SKILL_SOURCE` 环境变量指向本仓库。

---

## 2. 技能发现（为什么放这个位置）

`skills@1.5.20` 的发现逻辑（`findSkillMdPaths`，已从 CLI dist 反编译核实）：

1. 先扫优先前缀：仓库根 `<dir>/SKILL.md`、`skills/<dir>/SKILL.md` 等。
   命中任何一个 → **只装优先命中项**。
2. 否则兜底：装所有路径深度 ≤ 6 段的 SKILL.md，目录名取 SKILL.md 的父目录名。

next 现状是兜底路径：`plugins/sealos/skills/use-sealos/SKILL.md`（5 段）。
git tree 里 `skills/use-sealos` 是 symlink blob，GitHub 安装路径**看不到**它。

推论（已用真实 CLI 逻辑对未来 git 文件清单仿真验证）：

- 新技能必须与 use-sealos 同层（`plugins/sealos/skills/<name>/`），三个一起走
  兜底 → GitHub 安装得到三个技能。
- **绝不能**把真实目录放进 `skills/`：那会触发优先前缀，use-sealos（不在优先
  位置）反而不装，直接打破现网行为。
- 本地路径安装走文件系统（symlink 展开）→ `skills/` 三个 symlink 命中优先
  前缀，同样装齐三个。两条路径均已实测（`npx skills@1.5.20 add`）。

安装后布局扁平化为 `.agents/skills/{use-sealos,sealos-deploy,k8s-kaniko-job}/`，
因此技能间用 `../use-sealos/...` 相对引用在安装后总是成立。

---

## 3. Brain 注入的运行环境（核实版）

Devbox 创建时一次性注入（resume 不重注）：

| 变量 | 值/含义 |
|---|---|
| `SEALAI_DEPLOY_MODE` | `managed` |
| `SEALAI_DEPLOY_TASK_ID` / `SEALAI_PROJECT_ID` | 任务/项目 id |
| `SEALAI_NAMESPACE` / `SEALAI_DEPLOY_NAMESPACE` | 目标命名空间 |
| `SEALAI_DEPLOY_WORKSPACE` | `/home/devbox/project` |
| `KUBECONFIG` / `SEALAI_KUBECONFIG_PATH` | `/home/devbox/.kube/config`（Devbox 平台写入，edit 角色） |
| `SEALAI_INPUTS_PATH` | `/run/sealai/deployment/inputs.json`（flat string map，0600） |
| `SEALAI_DEPLOY_LABELS_JSON` | `{"brain.io/managed-by","brain.io/project-id","brain.io/deployment-kind"}` — 无 task-id、无 deployment-name |
| `SEALAI_TURN_DEADLINE_AT` | 总 70 分钟截止（创建时定格） |
| `SEALAI_DEPLOY_MCP_TOKEN` | MCP bearer（禁止打印） |
| `GITHUB_TOKEN` | GitHub 源注入；克隆 + GHCR 推送用 |
| `SEALAI_CONTRACT_DIR` | `.sealos/brain` — Brain 自己不读不写，纯禁区 |

Brain 每轮会把 `.sealos/build-runtime.json` 写进 workspace（Kaniko 合同）：

```json
{
  "accessKeyId": "admin",
  "bucket": "kaniko-context",
  "region": "sealos-internal",
  "s3Endpoint": "http://<devbox-network-id>:1319",
  "secretKeyRef": { "key": "SEALOS_DEVBOX_JWT_SECRET", "name": "<devboxName>" },
  "buildDeadlineAt": "<ISO>", "buildDeadlineSeconds": 1800,
  "devboxName": "...", "workspaceDir": "/home/devbox/project"
}
```

GitHub 源任务若拿不到 devbox network id，Brain 在 Codex 启动前就失败
（`build-runtime-unavailable`）——构建通道是这条链路的先决条件。

Devbox 运行时（labring-actions/devbox-runtime sandbox/v1）自带 VersityGW：
以 POSIX 目录当 S3，监听 0.0.0.0:1319，并向 Codex 会话导出
`S3_ENDPOINT`、`KANIKO_CONTEXT_POSIX_DIR`、`KANIKO_CONTEXT_S3_BUCKET=kaniko-contexts`
等变量。注意 Brain 合同里的 bucket 是 `kaniko-context`（单数）而运行时默认
`kaniko-contexts`（复数）——`kaniko-build.py` 对 URI 和 POSIX 写入路径取**同一个**
解析结果（env 成套优先，其次 Brain 合同，最后默认值），两名不一致不会造成写读错位。

---

## 4. MCP 合同（核实版，含此前遗漏的护栏）

端点 `POST /api/deploy-agent/mcp/v1`，bearer 即 `SEALAI_DEPLOY_MCP_TOKEN`，
仅两个工具，输入 schema **strict**（多一个字段就报错）。

### `template_ready`

- 输入：仅 `{ sha256 }`，小写 hex，对 `.sealos/template/index.yaml`
  **最终文件字节**计算（Brain 读同一路径重新计算比对）。
- Brain 只验模板头：首个 `---` 之前必须是 `apiVersion: app.sealos.io/v1` /
  `kind: Template` / 非空 `metadata.name`，其后必须有资源文档。
  **不渲染 defaults、不求值 `${{ }}`**（`random(8)` 留给 Template API）。
- 阻塞判定：可见 input 且 `required: true` 且无默认值且无已提交值 →
  `awaiting_user`，任务转 blocked，Agent 必须立即停轮。
- 用户提交后同线程恢复；此后**不得改动 `spec.inputs`**
  （`input_schema_changed_after_submission`）。

### `deployment_completed`

- 输入：`workloads` 1–32 个 `{apiVersion, kind, name, namespace}`（可选 uid），
  可选 `publicUrl`。namespace 会被 Brain 强制覆写成任务命名空间。
- Brain 在**同一个 Devbox 里** `kubectl get` 复核。就绪规则按 kind 定义；
  **硬规则：至少一个 Deployment/StatefulSet/DaemonSet/Job/Pod 就绪**，
  只报 Instance/App/Cluster 会判失败。
- `publicUrl`：Brain 从控制面探测，域名必须是租户域（`AP_USER_DOMAIN` 或
  用户 kubeconfig 主机名）本身或其子域，重定向全程不得出域，要求 2xx + 非空 body。
- 护栏错误码（skill 必须会处理）：`deployment_completed_before_template_ready`、
  `deployment_completed_throttled`（两次调用间隔 ≥5s）、
  `invalid_template_digest`、`template_digest_mismatch`。
- `repair` 无次数上限，唯一限制是总截止时间；修复必须**原地收敛**，禁止二次
  Template API 全量重部（会造出第二个 Instance）。
- 轮次结束既没有 `awaiting_user` 也没有 `deployment_completed` →
  completion-required 重试，**最多 2 次**后判 runner-error。

`extraLabels`：Brain 的 prompt 要求 Agent 把 `SEALAI_DEPLOY_LABELS_JSON` 原样
交给 Template API（labring/sealos-skills `main` 的“禁止 extraLabels”是那边的
合同裂缝，与 Brain 现行 prompt 相悖；next 按 Brain prompt 执行）。

---

## 5. 本仓库落地清单（已完成）

| 文件 | 内容 |
|---|---|
| `plugins/sealos/skills/sealos-deploy/SKILL.md` | **新建**。managed 总入口：模式门、环境表、硬规则（禁登录/全程非交互/禁 token 泄露/禁文件 RPC/不许无回调收轮）、五步流水线（分类 → Kaniko 构建 → 固定路径模板 → template_ready 握手 → 部署+验证+deployment_completed）、repair 语义、路由表指回 use-sealos references |
| `plugins/sealos/skills/k8s-kaniko-job/SKILL.md` | **新建**。构建执行器说明：机制、前置条件、结果读取（digest 钉扎、pull 分类与 pull secret）、失败分诊表 |
| `plugins/sealos/skills/k8s-kaniko-job/scripts/kaniko-build.py` | **新建**，stdlib-only 单脚本：解析 build-runtime.json + 运行时 env → 校验 GHCR token（write:packages、owner=login）→ tar 上下文进 VersityGW POSIX 目录 → 现场造 registry secret（S3 凭证优先直接引用 Brain 给的 devbox secret，不落盘）→ 渲染并 apply Kaniko Job（`--digest-file=/dev/termination-log`、backoffLimit 0、deadline ≤1800s 且尊重 Brain 截止时间、ttl 3600）→ wait → 从 termination message 取 digest → 匿名拉取分类 → 输出 JSON。支持 `--render-only` 离线验证 |
| `plugins/sealos/skills/use-sealos/SKILL.md` | Preflight 最前加 managed 门（转 `../sealos-deploy/SKILL.md`）+ Routing 一行 |
| `plugins/sealos/skills/use-sealos/scripts/sealos-api.py` | ① `deploy` 请求体可选 `extraLabels`（`--labels-json` 或 `SEALAI_DEPLOY_LABELS_JSON`，无则省略字段）② kubeconfig 解析：`SEALOS_KUBECONFIG` > 已存在的 `~/.sealos/kubeconfig` > 已存在的环境 `KUBECONFIG`；`login`/`switch` 永不写环境 `KUBECONFIG` ③ 新增 `store-export <template> --out F`：把 store 模板源（Template CR + 资源）落成单文件并输出 sha256——managed 下 store 路径由此物化到固定路径再走 raw deploy（`deploy-store` 在 managed 下禁用：无法带 extraLabels、无本地字节可哈希） |
| `skills/{sealos-deploy,k8s-kaniko-job}` | 新 symlink |
| `README.md` | 目录树、skills.sh 三技能说明、Brain 一节 |
| `test-host-coverage.js` | 新增 Brain pack 测试：两个 marker 目录名、symlink 指向、SKILL.md 合同要素、use-sealos 的门 |

### 关键取舍

- **单脚本而非 markdown 编排**：sealos-skills 的 kaniko 是 6 个脚本 + 7 个
  模块 md 由模型逐步编排，出错面大。next 收敛为一个可离线测试的
  `kaniko-build.py`，SKILL.md 只讲怎么调、怎么读结果、怎么分诊。
- **digest 钉扎**：采用 brain-deploy-preview 分支的 termination-log 方案
  （unify 分支砍掉了 digest，模板只能钉 tag）。
- **S3 凭证零拷贝**：Brain `secretKeyRef` 指向 devbox 现成 secret，Job 直接
  `secretKeyRef` 引用；仅当合同缺失时才从 env 现场造 secret。

---

## 6. 已验证（离线可证的全部）

1. `npx skills@1.5.20 add <本仓库>`（本地路径，实跑）→ 恰好安装
   `use-sealos` / `sealos-deploy` / `k8s-kaniko-job` 三个。
2. GitHub 安装路径：用 CLI 真实 `findSkillMdPaths` 逻辑对未来 git 文件清单
   仿真 → 同样三个（兜底路径，无优先前缀遮蔽）。
3. Brain 安装脚本 marker 检查逐字模拟 → 通过；安装后
   `../use-sealos/scripts/sealos-api.py`、`../k8s-kaniko-job/scripts/kaniko-build.py`
   等 sibling 相对路径全部存在。
4. `kaniko-build.py --render-only`（喂 Brain 格式 build-runtime.json）→ Job
   YAML 经 yaml 库解析合法：deadline 钳制、devbox secret 引用、digest-file、
   S3 端点正确；过期 deadline / 非 ghcr 镜像 / 缺 tag / 非法 build-arg 均正确报错。
5. `sealos-api.py`：打桩 HTTP 后端到端验证 `deploy` 请求体——有
   `SEALAI_DEPLOY_LABELS_JSON` 时带 `extraLabels`，无则省略；kubeconfig 三级
   优先级 + 写路径隔离；`store-export` 产物结构与 sha256 一致性。
6. 仓库测试 `node --test test.js test-host-coverage.js` 7/7 通过；
   两个 python 脚本 `py_compile` 通过；本地 use-sealos 行为不变
  （无 managed 变量时门不触发，kubeconfig 解析在 `~/.sealos/kubeconfig`
   存在时与旧行为逐字节一致）。

## 7. 待线上验收（无法离线证明）

1. Brain 测试环境 `DEPLOY_SKILL_SOURCE` 指向本仓库，跑一次 GitHub 源码部署
   （覆盖 Kaniko 构建 + 握手 + 表单 + 完成复核）。
2. 跑一次 store 模板命中的部署（覆盖 `store-export` → raw deploy 路径）。
3. Template API 对 raw deploy 的 `extraLabels` 实际打标效果（平台能力）。
4. `publicUrl` 域名与 `AP_USER_DOMAIN` 的匹配情况——skill 已内置退让：Brain
   报域外/不可达而 workload 健康时，重报一次不带 `publicUrl`。
5. 生产 `DEPLOY_SKILL_SOURCE` 何时从 `labring/sealos-skills#main` 切换。

---

## 8. 关键文件索引

Brain（只读参照，勿改）：

- `apps/ui/src/features/deploy/task/runner.ts` — 安装命令、marker、env、完成观察、就绪规则
- `apps/ui/src/features/deploy/task/gateway-prompt.ts` — Agent 指令原文
- `apps/ui/src/features/deploy/task/managed-deployment-contract.ts` — MCP schema/配置
- `apps/ui/src/features/deploy/task/build-runtime-contract.ts` — Kaniko S3 合同
- `apps/ui/src/app/api/deploy-agent/mcp/v1/route.ts` — 鉴权、限流、错误码
- `docs/adr/0037-...md`、`docs/testing/github-deploy-smoke.md` — 验收口径

sealos-skills（机制蓝本）：

- `origin/brain-deploy-preview:skills/k8s-kaniko-job/` — digest 捕获版 Kaniko（本仓库脚本的蓝本）
- `codex/unify-main-brain-deploy:skills/k8s-kaniko-job/` — 无 digest 简化版

devbox-runtime：`base-images/frameworks/sandbox/v1/{versitygw,codex-gateway}/run`
— VersityGW 布局与注入 env 的事实来源。

---

## 9. 已否决/修订的旧结论

| 旧结论 | 修订 |
|---|---|
| “不拆 sibling、Brain 改安装器”（旧 §8.1/§8.4） | Brain 不可改 → marker 决定必须有 `sealos-deploy` + `k8s-kaniko-job` 两个技能目录。kaniko 仅作 reference md 满足不了 marker |
| “Kaniko 推迟”（旧 §12） | GitHub 源码构建在 Devbox 里没有 Docker daemon，Kaniko 是唯一构建通道，且 Brain 把它设为任务先决条件 → 本次必须做 |
| “合同写进 use-sealos/references/brain.md” | 合同即 `sealos-deploy/SKILL.md` 本体（Brain prompt 直接点名跑这个技能，无需二跳） |
| “Path A 用 deploy-store” | managed 下改为 `store-export` 物化 + raw deploy（Brain 需要本地字节算 SHA、需要 extraLabels） |
| 靠 `brain.io/task-id` 扫集群 | 维持否决：完成识别是 MCP `workloads[]`（ADR 0037） |
