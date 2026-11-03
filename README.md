# GLaDOS 自动签到

按 2026 年浏览器真实请求写的极简签到脚本：`POST https://glados.rocks/api/user/checkin`，body 为 `{"token":"<host>"}`。

- **零依赖**：只用 Python 标准库（`urllib`），无需 `pip install`
- **IQI 四档签到**：16:00 `<80` / 17:00 `<85` / 18:00 `<90` / 19:00 任意 Index 兜底，优先拿 Cable +8
- **多域名兜底**：依次尝试 `glados.cloud` / `glados.rocks` / `glados.network`
- **两种部署**：Windows 计划任务，或 GitHub Actions

认证只靠登录 Cookie（`koa:sess` / `koa:sess.sig`）。**Cookie 只放 `.env` 或 GitHub Actions Secrets，绝不提交进仓库。**

> 网上很多老脚本仍请求 `glados.cloud` / `glados.one`，可能和当前控制台不一致。以你自己抓到的 host / token 为准。

当前积分规则不是早年的「签到直接加会员天数」，而是：

**每天签到赚 Points → 攒到 500 分兑换 100 天。**

---

## 目录结构

| 文件 | 作用 |
| --- | --- |
| `checkin.py` | 主脚本，零依赖 |
| `install-task.ps1` | 注册 Windows 计划任务 |
| `.github/workflows/checkin.yml` | GitHub Actions 定时任务 |
| `.env.example` | 环境变量模板 |
| `checkin.log` | 运行日志（已 gitignore） |

---

## 积分怎么算

官网说明：积分受连续签到、套餐等级、当前海底光缆质量以及随机因素影响；不同站点还可能有不同奖励。完整数值公式没有公开。

从控制台展示和实测，可以拆成：

```text
每日积分 = Base + 套餐奖励 + 连签奖励 + 海缆奖励
```

| 部分 | 机制 | 已知情况 |
| --- | --- | --- |
| Base | 签到前会显示 `+X`，有随机 | 实测出现过 `+1` |
| Plan | 套餐等级加成 | Basic 为 `+0`；更高套餐应有额外积分 |
| Streak | 连续签到加成 | 连签存在，但**不是**「每天连签就 +N」。实测连续 2 天仍是 `Streak +0`，更像达到 N 天门槛后才加成。网上若直接给出 3/7/14/30 完整表，暂不宜当成官方规则 |
| Cable | 海缆质量越差，奖励越高 | 实测 Cable Index 约 77–78、状态 Poor 时为 `Cable +8`。官网也写了：NCP/APG 等海缆维护、拥塞或损坏导致网络质量下降时，Submarine Cable Observer 会拿到更多积分 |
| 合计 | 以上相加后入账 | 例如 `1 + 0 + 0 + 8 = 9` |

因此「线路越差，签到积分越高」不是装饰文案，而是真正在算分。

同一天重复请求只会返回已签到（`code=1`），不会拿两份积分。更合理的是每天一次，失败再重试。

参考：[GLaDOS 控制台](https://glados.network/console)

---

## 兑换：只换 500 → 100 天

兑换档位是公开的：

| 积分 | 会员天数 | 每天成本 | 利用率（天/point） |
| ---: | ---: | ---: | ---: |
| 100 | 10 天 | 10 points/day | 0.10 |
| 200 | 30 天 | 6.67 points/day | 0.15 |
| **500** | **100 天** | **5 points/day** | **0.20** |

500 档的积分利用率刚好是 100 档的 2 倍。

**不要 100 / 200 分就换，一律攒到 500 再换 100 天。**

若海缆长期较差、每天大约 +9，从 50 分攒到 500 大约还要 50 天量级（实际每天会波动）。

更关键的是可持续续期：只要长期日均积分达到约 `5 points/day`，100 天就能攒满 500 分，再换回 100 天，积分收支近似打平。

本脚本**不会自动兑换**，避免误用 100/200 档。

参考：[NodeLoc 讨论](https://www.nodeloc.com/t/topic/97182)

---

## IQI 智能签到

优先拿 **Cable +8**（Index < 80），若未签上则逐档放宽；**19:00 任意 Index 兜底**，保证不断签。

| Network Index | 每日 Cable Bonus |
| ---: | ---: |
| **< 80** | **+8** |
| 80–84 | +5 |
| 85–89 | +3 |
| 90–95 | +2 |
| > 95 | +1 |

每天 **4 次**计划任务（全部**严格小于**，等于阈值不签，最后一档除外）：

| 北京时间 | 条件 |
| --- | --- |
| **16:00** | Index **< 80** |
| **17:00** | Index **< 85** |
| **18:00** | Index **< 90** |
| **19:00** | **anyscore**（任意 Index，确保今日签到） |

判断顺序：窗口检查 → 今日是否已签 → 查询 `/api/user/iqi` → 按上表决定是否调用签到。IQI 失败时不签，等下一档。

示例：Index 全天 82 → 16:00 不签（≥80）→ 17:00 签（<85）。若 16–18 点都未签，19:00 仍会签到以免断签。

阈值逻辑可单独自测：

```powershell
py -3 checkin.py --test-threshold
```

---

## 建议策略

1. 每天签到（本地计划任务或 GitHub Actions）
2. 不兑换 100 / 200 档
3. 攒到 500，换 100 天
4. Cookie 大约 300 天过期；失效后从浏览器重新复制到 `.env` 或 Secrets

若要继续反推 Streak / Cable 分段，建议自己按天记录（不要把 Cookie、邮箱、账号 ID 写进仓库）：

```text
日期, Cable Index, Cable 状态, Base, Plan, Streak 天数, Streak 奖励, 入账积分
```

等连签到 3 / 7 / 14 / 30 天附近，比较容易看到 Streak 是否跳档。

---

## 本地使用

1. 复制 `.env.example` 为 `.env`
2. 打开控制台签到页：`F12 → Network → 手动签到一次 → checkin 请求 → Request Headers → Cookie`
3. 把 Cookie 贴进 `.env` 的 `GLADOS_COOKIE`
4. 运行：

   ```powershell
   py -3 checkin.py
   ```

可选环境变量（见 `.env.example`）：

| 变量 | 说明 |
| --- | --- |
| `GLADOS_COOKIE` | 必填。浏览器 checkin 请求里的整段 Cookie |
| `GLADOS_HOST` | 可选。默认 `glados.rocks`；body 的 token 会跟着这个 host |
| `GLADOS_PROXY` | 可选。不填则自动探测本机 `7897 / 7890 / 10809 / 1087`；GitHub Actions 上留空走直连 |

接口返回约定：

- `code=0`：签到成功
- `code=1`：今天已经签过
- 其他：失败（常见是 Cookie 过期）

### Windows 每天自动跑

```powershell
powershell -ExecutionPolicy Bypass -File .\install-task.ps1
```

默认定时 **16:00 / 17:00 / 18:00 / 19:00** 四档；已开启 **唤醒计算机以运行此任务**。日志见 `checkin.log`。

注意：仅对**睡眠**有效，关机/休眠后无法唤醒；部分笔记本需在 BIOS 或电源选项里允许「定时器唤醒」。

卸载：

```powershell
Unregister-ScheduledTask -TaskName GLaDOS-Checkin -Confirm:$false
```

### GitHub Actions

仓库可以公开，但 `GLADOS_COOKIE` 只能放在：

`Settings → Secrets and variables → Actions`

不要把 `.env` 或 `checkin.log` 推上去。Workflow 与本地一致，在北京时间 **16:00 / 17:00 / 18:00 / 19:00** 各跑一次；GitHub 可能延迟，本机计划任务更稳。

---

## 免责声明

本项目仅用于个人学习与自动化自己的账号签到，请遵守 GLaDOS 服务条款，风险自负。不接受也不鼓励用于批量或他人账号。

## 许可证

[MIT](LICENSE)
