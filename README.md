# Option Vault

在常开 Mac mini 上积累**免费延迟美股期权快照**，支持永久存储、Telegram 数据获取报告，以及只读的卖方研究网站。网站提供实验性比较分与历史股价情景统计，不进行交易或承诺未来收益。

## 设计与文件结构

```text
config.example.json           可复制的完整配置，不含凭据
option_vault/
  sources/
    http.py                   超时、间隔、退避、429、请求归档
    cboe.py                   主源；可选独立报价接口
    yahoo.py                  yfinance 备用链、日线、财报和除息日
  parsing.py                  集中字段映射、OCC 解析、数值标准化
  models.py                   统一 Chain 模型及 Source 协议
  calculations.py             卖方区间、衍生值、质量标记、标准化 IV
  storage.py                  SQLite 事务、原始 JSON gzip、在线备份
  schedule.py                 NYSE 日历、美东时间、提前收盘
  notifications.py            正常报告、即时告警、发送失败计数
  runner.py                   单标的故障隔离、源切换、漏跑检测、每日汇总
  cli.py                      手动采集、launchd tick、备份、IV 指标
launchd/                      开机启动的 LaunchDaemon plist 示例
scripts/                      launchd 入口、备份脚本
tests/                        解析、计算、调度、存储、通知和编排测试
```

选择 SQLite：无需服务进程，适合单机十个标的定时写入；支持事务、索引、WAL、在线备份，便于以后导出 Parquet。数据库、原始响应、日志均不设过期删除。磁盘用量需要自行监控。

### 已核实的 Cboe 结构

2026-11-16 实际访问 SPY 的两个公开接口，HTTP 均为 200：

```text
https://cdn.cboe.com/api/global/delayed_quotes/options/SPY.json
https://cdn.cboe.com/api/global/delayed_quotes/quotes/SPY.json
```

响应顶层为 `timestamp`、`symbol`、`data`；链在 `data.options`，现价为 `data.current_price`。合约有 `option`、`bid`、`ask`、`last_trade_price`、`volume`、`open_interest`、`iv`、`delta`、`gamma`、`theta`、`vega`。到期日、C/P、行权价从 OCC 尾部解析。所有这些字段名集中在 `parsing.py`，结构缺失会报错，不会当作空链成功。

响应没有给时间戳时区。配置**显式假定无偏移时间戳为 UTC**，不是接口的官方保证；可修改 `sources.cboe_timestamp_timezone`。保留原始时间戳供核对，未知或超前也会标记。样本 IV 是小数（如 0.1565），不把它当百分数再除以 100。独立报价与期权链时间可能不同，默认用链自带现价，不额外请求独立报价。

**精确使用配置代码，不做别名映射。SPCX 不会替换成 SPAX。** 两源响应的标的代码都必须完全匹配。SPCX 的公司身份和是否有上市期权不能由用户提供的名字保证；代码不臆断，如果接口没有该链会正常记录失败并告警。Cboe 可能包含特殊根代码的合约，保留原始 OCC，不把它们重写成普通合约；简单年化公式不理解调整合约的交割物。

## 1. 安装

建议使用 Python **3.11 或 3.12**（带现代 OpenSSL），不要依赖 macOS 自带的 LibreSSL Python。兼容 Python 3.9，但其交易日历限制到 4.x，长期使用建议升级 Python 以使用更新日历。

```sh
cd /绝对路径/option-vault
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
cp config.example.json config.local.json
```

`data_dir` 相对**配置文件所在目录**解析；可改为容量充足的绝对路径。配置示例中的标的就是：

```text
SPY QQQ AAPL MSFT NVDA TSLA GOOGL AMZN SPCX SBET
```

标的大小写必须准确，无自动纠错。两个接口都是非官方资源，免费不代表稳定性保证；请遵守数据提供方条款，仅用于自己的学习。更新依赖后先执行测试和一次手动采集。日历还可能有临时停市，应定期更新依赖。

## 2. Telegram 凭据

1. 在 Telegram 中通过官方 `@BotFather` 创建 bot，获取 token。
2. 私聊 bot 发送 `/start`，或将 bot 加入目标群；群内可能需要允许 bot 发消息。
3. 通过 Telegram Bot API 的 `getUpdates` 获取目标消息的 `chat.id`；群 ID 可能为负数。不要将返回内容或 token 提交到仓库，也不要把含 token 的 URL 贴到日志、截图或聊天。
4. 推荐使用**仓库外**文件 `~/.config/option-vault/telegram.json`：

```json
{"bot_token": "填写实际token", "chat_id": "填写实际chat id"}
```

```sh
mkdir -p ~/.config/option-vault
chmod 700 ~/.config/option-vault
# 用本地编辑器创建上面的文件，然后：
chmod 600 ~/.config/option-vault/telegram.json
```

也可设置 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`，环境变量优先。不依赖 `.env` 自动加载。launchd 不继承终端 shell 环境，所以开机任务建议用本地凭据文件；文件必须由运行任务的普通用户读取，权限不得开放给组或其他用户。

启用 Telegram 却缺少凭据会明确失败；手动离线验证请加 `--no-telegram`，不要填假 token。

发送失败不影响采集；持久记录失败次数，下次发送成功的消息附带说明。正常十标的报告是一条消息；特别长的错误报告按 Telegram 长度限制拆分。不发送行情分析。

## 3. 首次运行

```sh
# 一个标的、单轮、真实网络请求，不发 Telegram
.venv/bin/option-vault --config config.local.json --no-telegram once --symbol SPY

# 多标的测试；--symbol 可以重复，必须已经在配置列表中
.venv/bin/option-vault --config config.local.json once --symbol SPY --symbol QQQ

# 所有配置标的，单次采集和 Telegram 报告
.venv/bin/option-vault --config config.local.json once

# 查看交易日安排，不请求行情；休市日不输出时点
.venv/bin/option-vault --config config.local.json schedule --date 2026-11-27

# 生产调度入口；非交易日/非应采窗口不采行情
.venv/bin/option-vault --config config.local.json tick

# 立即测试指定期权报告；加 --no-telegram 仅写日志，不发消息
.venv/bin/option-vault --config config.local.json watch-once
```

全局参数 `--config`、`--no-telegram` 放在子命令前。`once` 可在非交易时间主动测试，但标签始终为 `manual-*`，不会冒充某个定时时点，也不会写每日收盘 IV。启动 `once` / `tick` 都会检查当日已过时点的漏跑。单标的最终失败会使 `once` 返回非零；辅助数据失败则保留成功的期权快照，状态为 warning。

### 指定期权每小时报告

`hourly_watch` 默认启用 `SBET280121C00008000`，即 SBET 2028-01-21 到期、行权价 $8 的 Call。交易日美东 10:00 至 15:00 的每个整点单独发送买价 Bid、卖价 Ask、最近成交价 Last、来源、采集时间及数据源时间戳/延迟；提前收盘日仅报收盘前的整点（13:00 收盘时为 10、11、12 点），休市日不报，到期日之后不再报。Last 是最近一笔成交价，并不一定是当前成交价，可能陈旧。报价为非官方延迟数据，未知、缺失、零买价和过旧时间会明确标记，不作交易建议。

指定合约按完整 OCC 代码精确匹配完整期权链，**不受一年到期和卖方区间限制**；Cboe 失败或未找到时可退回 yfinance 的指定到期日。找不到合约或两源失败会发送失败告警，不会选相近合约。单独请求与原有整轮采集相互独立，先执行指定合约报告，可能增加接口请求和运行时长；请求退避过长时原有采集仍可能漏过宽限窗口，漏跑检测会提示。定时报表使用 `watch-HH:MM` 标签，记录到 `runs` 和 `watch_quotes`，原始响应照常归档；重启后缺失的报表会提示但不回补。手动测试用 `watch-manual-*` 标签，不占用定时报告。请先按下文安装并启动 launchd，否则不会自动定时运行。

首次会通过 yfinance 取最近一个月日线和最新事件信息；不是回补期权历史。每交易日首次成功后不重复取辅助数据，收盘快照再刷新一次，以替换当日尚未收盘的日线。失败会告警并在以后时点继续尝试；财报/除息日尚未公布或 ETF 无财报时保持 null，不捏造日期。事件接口失败但日线成功时仍保存日线，事件记录带 fetch_errors，单独告警，不伪装成完整成功。

## 4. 调度、漏跑及重启

- 使用 `America/New_York`，通过 NYSE 交易日历处理周末、休市、夏令时和提前收盘。
- 正常交易日：10:00、11:00、12:00、13:00、14:00、15:00、15:50、16:15-close。
- 提前收盘：将七个盘中时点按**实际交易时长 / 6.5 小时**缩放到实际开盘后，向下取整到分钟；另取实际收盘后 15 分钟。
- 2026-11-27 示例：09:46、10:18、10:50、11:23、11:55、12:27、12:54、13:15-close。
- 每分钟被 launchd 唤醒一次，默认允许时点后 8 分钟内启动。宽限期内开始的是延后实采，UTC/ET 采集时间如实保存；盘中快照不在实际收盘后启动。
- 超出宽限的缺失时点告警，但**不以当前行情补写过去**。不会补跑前一天的期权快照。
- SQLite 记录时点是否已尝试；失败后等下个时点，不在同一窗口每分钟重复请求。文件锁避免手动、定时和备份互相重叠。
- 获取锁之后才能把遗留 running 标记为 interrupted，因此不会把另一个正在工作的进程误判为崩溃。断电/强杀无法当场通知，下一次启动会报告中断。
- 每次正常收盘轮完成后发每日汇总；若收盘轮漏跑，稍后 tick 仍会发出缺失汇总。包括各时点状态和未记录标准化 IV 的标的。status=ok 表示所有标的期权均已落盘，其中仍可能有 warning；partial 表示部分标的失败。
- 每个标的依次抓取，不承诺十个标的同一秒。Yahoo 备用需要逐个到期日请求，可能较慢；如果一轮太长导致后续时点漏跑，会明确报告。

### LaunchDaemon（推荐：无需登录，开机自动恢复）

项目和数据必须位于任务用户可以访问的本地路径，不要放在需要 GUI 登录后才挂载的目录。plist 使用普通用户身份，**不要让采集器以 root 身份运行**。

1. 编辑 `launchd/com.optionvault.collector.plist`：
   - 把所有 `/ABSOLUTE/PATH/option-vault` 换为项目绝对路径。
   - 把 `YOUR_MAC_USERNAME` 换成 `id -un` 的输出。
   - 建议同时把配置的 `secrets_file` 改为凭据文件的绝对路径，避免开机环境 HOME 与终端不同。
   - 项目目录、虚拟环境和配置必须在开机时可读。launchd 不展开 `~`、shell 变量或相对路径。
2. 安装：

```sh
mkdir -p data/logs
plutil -lint launchd/com.optionvault.collector.plist
sudo cp launchd/com.optionvault.collector.plist /Library/LaunchDaemons/com.optionvault.collector.plist
sudo chown root:wheel /Library/LaunchDaemons/com.optionvault.collector.plist
sudo chmod 644 /Library/LaunchDaemons/com.optionvault.collector.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.optionvault.collector.plist
sudo launchctl kickstart system/com.optionvault.collector
sudo launchctl print system/com.optionvault.collector
```

修改 plist 后，先 `sudo launchctl bootout system/com.optionvault.collector`，再 bootstrap。暂停/卸载同样先 bootout，然后手动移除已安装的**这个** plist 文件。不要使用 `kickstart -k` 在采集中强杀进程。

LaunchAgent 备选：移除 plist 的 `UserName`，安装到 `~/Library/LaunchAgents/` 并用 `launchctl bootstrap gui/$(id -u) ...`；**只有该用户登录后才会启动**，不符合无人登录开机运行要求。

`scripts/launchd-run.sh` 把解释器启动错误和标准输出写到项目 `data/logs/launchd-YYYY-MM-DD.log`（UTC）。应用日志在配置数据目录的 `logs/collector.log`，按 UTC 日期轮转，永久保留。项目日志目录与自定义数据目录可能不同。

Mac mini 的“屏幕关闭”不影响运行，但**系统睡眠会漏采**。可在系统设置中启用防止自动睡眠和断电恢复；按自己的电源策略决定是否使用 `sudo pmset -a sleep 0 autorestart 1`（会改变系统电源配置，不由安装脚本自动执行）。FileVault 开机解锁、网络不可用、外置盘尚未挂载都可能阻止运行；launchd 不能绕过磁盘加密，也不能保证关机时采集。建议先重启演练并检查日志及 Telegram。

## 5. 范围、衍生值、质量

参数均在配置中；先做采集范围选择，再做质量标记：

- Call/Put 均取，从美东当日到**下一年同一日期**，含周权和当日到期。
- 仅保留虚值方向（Call strike ≥ spot；Put strike ≤ spot）。
- 有 Delta 时，默认 `abs(delta)` 在 `[0.05-0.02, 0.5+0.02]`。
- 无 Delta 时，使用 `abs(log(strike/spot)) <= moneyness_sigma × IV × sqrt(max(DTE,1)/365)`；IV 无效使用配置中的 `fallback_iv`。高 IV、远月自然更宽，不为特定代码硬编码范围。这只是无 Greeks 情况下的宽松范围近似，不是假装计算 Delta。
- mid=(Bid+Ask)/2；任一缺失则 mid=null。Last 仅存储，不参与计算。
- DTE 为美东日历天数；年化=mid/strike×365/DTE；DTE=0 时年化为 null。
- spread=abs(Ask-Bid)；spread_ratio=spread/mid；mid≤0 时比例为 null。

**范围内全部保留，不因质量差而删除。** flags 是 JSON 数组，可同时有：

| 标记 | 含义 |
|---|---|
| zero_bid | Bid=0 |
| missing_quote | Bid 或 Ask 缺失 |
| missing_or_zero_iv | IV 缺失/非正 |
| missing_greeks | 至少一个 Greek 缺失（Yahoo 备用正常存在） |
| missing_oi / low_oi | OI 缺失 / 小于配置阈值 |
| wide_spread | spread_ratio 超阈值 |
| invalid_quote | 负报价或 Ask<Bid |
| missing_source_timestamp | 没有可证实的报价时间 |
| stale_timestamp / future_timestamp | 报价时间过旧 / 超前 |

OI 日内相同**不算异常**。配置 `quality` 控制 OI、价差、过旧时间、大面积缺字段、合约数下降、零 Bid 比例等阈值；45 分钟陈旧阈值已经给正常约 15 分钟延迟留了余量。合约数与最近成功轮及最近一个更早交易日同时点对比，默认少于原值一半告警；到期换月等正常原因也可能触发，告警是采集质量提示而非交易信号。

主源重试耗尽会立即告警，再按配置切到 Yahoo；备用成功也会明确标记。单标的失败不影响其他标的。默认首次+2次重试，5/10秒指数退避；请求之间至少2秒。HTTP429 告警并暂停至少300秒，若 Retry-After 更长则遵守，不反复硬撞。Yahoo 的认证状态由库自身管理；其 cookie 引导 404 不等于行情失败。

Yahoo 未提供 Greeks，也没有可靠的整条链报价时间，因此 Greeks 和 source_timestamp 均为 null，延迟报告为“未知”；**不把 lastTradeDate 或标的最近成交时间冒充期权报价时间**。

## 6. 标准化 IV 与历史指标

仅在交易日收盘标签采集时计算，来源是本轮**完整期权链**，不是已经筛过 Delta 的卖方区间：

1. 每个到期日选距离现价最近的行权价，距离使用绝对 log-moneyness，且不超过配置上限。
2. 同一 ATM 行权价有有效 Call/Put IV 时取中位数（两者即均值）；记录实际合约代码、方向、行权价和 IV。只有一侧有效时用该侧。
3. 取目标 30 日前后最近的两个到期日，对 **IV 本身按日历 DTE 线性插值**，不是方差插值；恰好30天则直接使用。
4. 不外推；缺两侧、无有效 IV、已知时间戳异常则不写，并告警。Yahoo 报价时间未知时可记录其 IV，但保留来源及 null 时间，报告有警示。
5. 每日每标的一行，以后从本项目实际记录开始积累。不会从股价波动反推历史 IV。

`iv_metrics(current, history, required=252)` 接收**不含当前日**的先前每日有效 IV。至少252个有效历史观测才输出：

- IV Rank = (当前值−历史最小)/(历史最大−最小)×100；
- IV Percentile = 历史中严格小于当前值的数量/样本数×100。

历史不足返回两个 null；历史完全相等时 Rank 为 null。Rank 可能超出0–100（当前值突破旧区间），不强行截断。CLI 从最新每日 IV 和它之前的数据库历史读取：

```sh
.venv/bin/option-vault --config config.local.json iv-metrics --symbol SPY
```

## 7. 数据与表结构

```text
data/
  vault.sqlite3                结构化数据；运行时可能有 -wal / -shm
  raw/YYYY-MM-DD/SYMBOL/LABEL/endpoint-UUID.json.gz
  logs/collector.log[.YYYY-MM-DD]
  .collector.lock
```

日期按美东采集日分区。每个完成的 HTTP 响应（包括失败状态和 Yahoo 逐到期日请求）在解析之前独立归档，包含请求时刻、去掉查询字符串的接口地址、HTTP 状态和原始 JSON 的 `body`。非 JSON 响应保留在 `body.non_json_body`。请求未收到响应时只能记错误，不可能存不存在的 JSON。为避免泄露，Yahoo 认证接口只存状态和脱敏说明，不存 crumb、cookie、请求头或查询参数。额外保留 yfinance 整理后的辅助数据 JSON。

重解析 Cboe：gzip 解压、读取 `body`，传给 `parse_cboe`；使用归档采集时刻而不是重新解析时的当前时间。原始存档不做质量或范围裁剪。

| 表 | 主键 / 关键字段 |
|---|---|
| runs | id；day、label、started/ended（UTC）、status、error |
| symbol_runs | run_id+symbol；status、source、contracts（实际保存数量）、elapsed（秒）、error、flagged_ratio、zero_bid_ratio、lag_seconds、warnings |
| contracts | run_id+symbol+occ_symbol；collected_utc、collected_et、source_timestamp、spot、expiry、strike、side（C/P）、bid、ask、last、volume、oi、iv、delta、gamma、theta、vega、source、mid、dte、annualized_yield、spread、spread_ratio、flags |
| daily_iv | day+symbol；iv、source、source_timestamp、target_days、inputs（JSON，含期限、行权价及实际合约）、run_id |
| daily_history | symbol+day；payload（Yahoo OHLCV、分红、拆股等原字段 JSON） |
| corporate_events | symbol；collected_utc、payload（最新事件日期；历史响应仍在 raw） |
| state | key；通知失败计数、辅助数据状态、漏跑/汇总去重状态 |
| watch_quotes | run_id；symbol、occ_symbol、collected_utc、source_timestamp、source、bid、ask、last；关联 `runs` 的 watch 标签 |

时刻为带偏移 ISO8601；IV/收益率/比例为小数，不是百分数。时间未知是 SQL NULL。每个标的的运行结果、合约和标准化 IV 在同一 SQLite 事务中提交；失败回滚，不出现报告保存了但只有一半合约的正常结果。

可直接用 macOS 的 sqlite3 查询：

```sh
sqlite3 data/vault.sqlite3 'SELECT day,label,status FROM runs ORDER BY started DESC LIMIT 10;'
sqlite3 data/vault.sqlite3 'SELECT day,symbol,iv,source FROM daily_iv ORDER BY day DESC LIMIT 10;'
```

SIGTERM/常规 Python 异常尽量立即发崩溃通知并记日志。SIGKILL、断电、解释器无法启动、磁盘完全不可写或网络断开时不能保证实时通知；重启恢复会检查遗留运行。Telegram 本身异常只记不含 token 的错误类型，并在下次成功时说明失败次数。

## 8. 备份与恢复

```sh
sh scripts/backup.sh /绝对路径/config.local.json /Volumes/另一块磁盘/option-vault-backups
# 或
.venv/bin/option-vault --config config.local.json --no-telegram backup \
  --destination /Volumes/另一块磁盘/option-vault-backups
```

每次生成独立 UTC 时间戳目录，完整复制原始响应和日志，SQLite 使用在线 backup API，**不直接复制正在使用的 WAL 数据库**；与采集共享锁，采集中则本次备份跳过并写日志。目标必须在数据目录之外，不清理旧备份。凭据文件在数据目录外，不会被备份。

可另建一个 launchd 任务每天运行上述脚本（建议美东收盘后较晚、非采集窗口）。例如复制采集 plist 为新名称，Label 改为 `com.optionvault.backup`，ProgramArguments 改为 `/bin/sh`、备份脚本绝对路径、配置绝对路径、目标路径；去掉 StartInterval，使用 `StartCalendarInterval`：

```xml
<key>StartCalendarInterval</key>
<dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>30</integer></dict>
```

该备份时间是 **Mac 系统本地时间**，上例为本地09:30，自行避开采集时段。开机运行 RunAtLoad 可按需要保留。备份任务最好也用包装脚本将标准输出和错误重定向到按日期命名的文件；命令输出为成功生成的目录。

恢复：先停用采集 launchd，保留当前数据目录作为另一个副本，把某次完整备份恢复到配置的数据目录（包括 `vault.sqlite3` 和 `raw`），再启动。不要把备份数据库覆盖到一个仍有旧 `-wal/-shm` 文件的活动目录；使用全新目录并修改配置最安全。

## 9. 验证

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
plutil -lint launchd/com.optionvault.collector.plist
sh -n scripts/backup.sh scripts/launchd-run.sh
```

测试使用固定响应结构和隔离数据库，不消耗接口流量、不发真实 Telegram。首次部署仍须手动真实采一轮，并验证自己的 Telegram 权限和 chat id；没有凭据时无法验证真实消息送达。未提供自动下单、保证金、多腿分析或历史期权回补。可视化研究网站见下节。

## 10. 可视化研究网站

### 启动和使用

```sh
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/option-vault-web --config config.local.json
```

默认地址 `http://127.0.0.1:8765`。使用 Waitress，不使用 Flask 调试服务器；只读打开采集库和研究库，不获取 Telegram 凭据、不占采集器文件锁、不修改期权数据。图表库从本地提供，不依赖外部 CDN。每60秒刷新当前页面；评分结果最多缓存60秒。日志为配置数据目录的 `logs/web.log`，按 UTC 日期轮转。缺少数据库或读取失败会返回明确错误，不伪造空结果。

页面：

- **策略排行榜**：选择卖 Put / 备兑 Call，按标的、DTE、历史日期/采集轮次筛选；调整四个评分权重，服务端重算全体候选排名。列表每页50个，散点图仅绘制本页。日期默认各标的最新成功快照；历史模式的DTE是报价时原始DTE，历史结果仍只使用当时以前的股价。过旧合约不再混入最新榜。
- **波动率概览**：每个标的最新标准化 IV、对应日期与来源、完整已积累 IV 曲线；IV Rank/Percentile 样本不足则明确标记。
- **期限探索**：先选标的和行权价，再查看各到期日的每股 mid 与 Bid/Ask 区间。未知或零报价不画成正常收益；列出已知缺失期限，不插值补造报价。点击图中的合约进入详情。
- **合约研究**：真实 Bid/Ask/mid/Last、IV、Greeks、成交量/OI 历史曲线，支持缩放、平移、图例切换。展示分项、历史比例、最不利5次情景和风险分布；备兑 Call 的股票下跌与错失上涨分开。
- **关注合约**：独立展示配置的 SBET 2028 $8 Call 每小时报价历史，不受普通一年期链范围限制，也不因此扩大普通链的采集。
- **采集与证据**：最近60次运行状态及股价历史实际覆盖范围。
- **评分方法**：完整公式、参数、限制，不隐藏实验性假设。

CSV 支持当前筛选的**全部结果（不止当前页）**、单合约完整采集历史、关注合约历史和全部标准化 IV。UTF-8 BOM 便于 Excel 打开；IV/收益率为小数、权利金为每股美元，UTC/ET 时间有明确列名；评分导出带权重、分项和可信度原因。缺失字段留空。防止字符串被表格软件作为公式执行。

### 全量股价历史（不是期权历史回补）

```sh
# 获取每个配置标的所有可用日线，明确失败并返回非零
.venv/bin/option-vault-history --config config.local.json
# 也可只刷新一个标的
.venv/bin/option-vault-history --config config.local.json --symbol SPY
```

单独保存在数据目录 `research.sqlite3`，不改动原有每日辅助数据任务和一年期权范围：

| 表 | 内容 |
|---|---|
| prices | symbol+day 为主键，close 为 Yahoo Close（拆股调整、不作分红再投资调整） |
| imports | 每个标的最后导入时间、实际首尾日期、条数、来源说明 |

全量导入成功时事务替换该标的整段序列，避免新旧拆股尺度混接；失败保留旧序列并记日志。每次行情HTTP响应仍归档。研究库不存在或该标的尚未导入时，网站使用已有的短期 `daily_history`，如实标记样本不足。默认不会把当天未完成日线或快照日及之后数据用于历史统计。历史上市代码可能承接完全不同的公司业务（如长期历史中的转型），统计未能自动识别所有制度变化，必须自行审阅实际覆盖期与风险案例。

建议每天美股收盘后刷新。`launchd/com.optionvault.history.plist` 为每天 **Mac系统本地09:00** 的用户级任务示例，调整路径和本机时区后安装；本机已安装相同任务，首次全量导入已完成。不是保证历史永不缺失，Yahoo 返回历史的起止可能因标的而异。价格历史不是历史期权报价，不能用来验证过去可成交权利金、提前平仓收益或提前指派。

### 实验性评分 v1

默认权重：收租效率45、历史结果20、尾部风险20、报价质量15；滑块允许0–100，按总和归一化，不能全部为零。未经过收益优化，不能视为最优权重。

- **效率**：`E = mid / B × 365 / 原始DTE`；Put 的 B=行权价，Call 的 B=标的现价。分项 `100E/(E+0.20)`。旧快照不会因为今天离到期更近而被动放大年化。临期高年化不代表全年可重复收取。
- **历史结果**：按该合约快照时的日历 DTE、`strike/spot` 比例，将全部可用历史分成非重叠起止区间。最早可用交易日为锚点；下个起点晚于前一终点；终点遇非交易日取此前最近交易日，最多容许4天误差。仅使用快照美东日期之前的收盘数据。Put 的终价/起价 ≥ strike/spot 算未进入价内；Call 用 ≤。分项=比例×100。
- **尾部风险**：Put 用 `max(0, strike/spot − 历史终价/起价)`，Call 用 `max(0, −历史股价收益率)`；以样本95分位 L 计算 `100/(1+L/0.20)`。不含权利金、费用，不是损益 VaR；Call 的错失上涨另外列示。
- **报价质量**：`100 × (1−min(1,spread/mid)) × min(1,OI/低OI阈值)`，其他时间和数据异常单列可信度。
- **总分**：有正权重分项的加权平均。必要报价/历史缺失时显示无法计算，不以零代填。只剩少量样本仍可显示参考分，但标记低可信度，默认不进排行榜。

低可信度包括：非今日报价、源时间陈旧/未知/超前、零或交叉报价、价差超过质量阈值、OI不足、非重叠样本少于20组、股价历史距报价日超过10天等。日期与陈旧判断基于当前美东日期，所以开盘前、周末或收盘后也可能没有符合默认门槛的候选；可勾选低可信度查看，不会用旧数据充当今日行情。

阈值放在 `config.example.json` 的 `web.scoring`，旧配置省略时使用同样的默认值，报价阈值复用 `quality`。修改配置后重启网站。历史比例的 Wilson 95% 区间仅作样本不确定性参考，**非重叠不代表独立，不能当作未来胜率区间**。不匹配当前波动环境，不自动建议建仓；历史最差情况不是未来最大损失。

### Tailscale 访问

默认只监听回环地址。优先使用仅 Tailnet 可见的 HTTPS：

```sh
tailscale serve --bg http://127.0.0.1:8765
tailscale serve status
```

如果 shell 找不到 `tailscale`，Mac App 版可用 `/Applications/Tailscale.app/Contents/MacOS/Tailscale` 替代。需要 Tailnet 管理员先启用 Serve/HTTPS；**不要使用 Funnel**（它会开放公网）。如机器已有 Serve 路由，不要覆盖它，先看 status，再给该站分配单独路径或端口。

本机 Tailnet 尚未授权 Serve，因此当前部署采用替代方案：同时绑定本地回环与本机 Tailscale IPv4：

```sh
.venv/bin/option-vault-web --config config.local.json --tailscale-ip "$(tailscale ip -4)"
```

参数只接受 `100.64.0.0/10` 中的地址，不接受 `0.0.0.0` 或普通局域网地址。本机当前入口 **http://100.102.136.83:8765**，同 Tailnet 且被 ACL/grants 允许的设备可访问；公网、普通LAN不直接提供监听。该HTTP连接在 Tailnet 隧道内传输，但浏览器层面不是HTTPS。没有第二套网站账号，访问控制依赖 Tailnet 的身份与网络规则；请仅授予可信设备访问。若节点IP变化，需要更新启动参数。以后启用HTTPS后可去掉额外IP监听。

### 网站常驻与启停

`launchd/com.optionvault.web.plist` 和 `scripts/web-run.sh` 是用户级 LaunchAgent 示例；替换绝对路径后安装。网站不与采集器共用Label。

```sh
cp launchd/com.optionvault.web.plist ~/Library/LaunchAgents/com.optionvault.web.plist
# 先用本地编辑器修改已复制文件里的绝对路径
plutil -lint ~/Library/LaunchAgents/com.optionvault.web.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.optionvault.web.plist
launchctl print gui/$(id -u)/com.optionvault.web
```

本机已安装并启动；已安装plist中增加了脚本第二个参数 `100.102.136.83`，由包装脚本传给 `--tailscale-ip`。默认仓库示例不含此机器专用地址。重启与停止：

```sh
# 重启网站，不影响采集服务
launchctl kickstart -k gui/$(id -u)/com.optionvault.web
# 停止并取消当前用户会话中的自动重启
launchctl bootout gui/$(id -u)/com.optionvault.web
```

网站 KeepAlive 自动恢复进程；SSH断开不影响，用户注销后会停止，重启后需要用户登录。无人登录运行应按采集器的 LaunchDaemon 方式以普通用户身份部署，并确保 Tailscale 网络和磁盘已就绪。`web-run.sh` 的解释器启动输出写项目 `data/logs/web-launchd-日期.log`；应用日志按日轮转永久保留。

研究历史定时任务安装方式相同（Label `com.optionvault.history`，每天本地09:00）。网站静态页面、Plotly资源与CSV下载都由同一进程提供，和 Telegram 没有依赖。备份命令会同时使用在线 backup API 备份 `vault.sqlite3` 与 `research.sqlite3`，两库各自一致但不是跨库同一瞬时事务；原始响应照常复制。
