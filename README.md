# GLaDOS 自动签到

按 2026 年浏览器真实请求写的极简脚本：`POST https://glados.rocks/api/user/checkin`，body 为 `{"token":"glados.rocks"}`。

认证只靠登录 Cookie（`koa:sess` / `koa:sess.sig`）。**Cookie 只放 `.env` 或 GitHub Actions Secrets，不要提交进仓库。**

网上很多老脚本仍请求 `glados.cloud` / `glados.one`，已经和当前控制台不一致。以你自己抓到的 host / token 为准。

当前积分规则也不是早年的「签到直接加会员天数」，而是：

**每天签到赚 Points → 攒到 500 分兑换 100 天。**

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

更关键的是可持续续期：只要长期日均积分达到约 `5 points/day`，100 天就能攒满 500 分，再换回 100 天，积分收支近似打平。2026 年社区也有人把这套「每日积分 + 500→100 天」理解成可循环续期机制。

参考：[NodeLoc 讨论](https://www.nodeloc.com/t/topic/97182)

本脚本**不会自动兑换**，避免误用 100/200 档。

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

接口约定：

- `code=0`：签到成功
- `code=1`：今天已经签过
- 其他：失败（常见是 Cookie 过期）

### Windows 每天自动跑

```powershell
powershell -ExecutionPolicy Bypass -File .\install-task.ps1
```

默认定时 09:17。日志写在 `checkin.log`（已 gitignore）。本机开了 Clash 一类代理时，脚本会探测 `127.0.0.1:7897` 等端口。

### GitHub Actions

仓库可以公开，但 `GLADOS_COOKIE` 只能放在：

`Settings → Secrets and variables → Actions`

不要把 `.env` 或 `checkin.log` 推上去。GitHub 定时任务可能延迟或漏跑，本机计划任务更稳。
