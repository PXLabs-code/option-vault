# Option Vault

在常开 Mac mini 上积累**免费延迟美股期权快照**，只做采集、永久存储及 Telegram 数据获取报告，不做交易或行情判断。

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

测试使用固定响应结构和隔离数据库，不消耗接口流量、不发真实 Telegram。首次部署仍须手动真实采一轮，并验证自己的 Telegram 权限和 chat id；没有凭据时无法验证真实消息送达。未提供自动下单、看板、保证金、多腿分析或历史期权回补。
