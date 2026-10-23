"""
config.py
集中管理全部可配置参数。所有其他模块必须从这里读取配置，
不允许在各模块内分散硬编码常量，便于审计和复现。
"""

import os
from dataclasses import dataclass, field
from typing import List


# ------------------------------------------------------------------
# 路径配置
# ------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
RESULTS_DIR = os.path.join(BASE_DIR, "results")
DB_PATH = os.path.join(DATA_DIR, "quant_research.db")
LOG_DIR = os.path.join(RESULTS_DIR, "logs")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)


# ------------------------------------------------------------------
# 通用网络配置
# ------------------------------------------------------------------

# SEC EDGAR 要求请求必须带有可识别的 User-Agent（含联系方式）。
# 请在实际部署时，通过环境变量 SEC_USER_AGENT 覆盖为你自己的真实邮箱，
# 不要在公开仓库里保留无意义的占位邮箱用于生产用途。
SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "quant-research-project contact@example.com"
)

HTTP_TIMEOUT_SECONDS = 20
HTTP_MAX_RETRIES = 3
HTTP_BACKOFF_BASE_SECONDS = 2.0

# CoinGecko 免费接口限流：粗略控制在每分钟不超过这个次数
COINGECKO_MAX_CALLS_PER_MINUTE = 20

# SEC EDGAR 请求速率限制（官方建议不超过约10次/秒，这里取更保守的值）
SEC_MAX_CALLS_PER_SECOND = 4


# ------------------------------------------------------------------
# 加密货币模块配置
# ------------------------------------------------------------------

@dataclass
class CryptoConfig:
    exchange_id: str = "binance"
    quote_currency: str = "USDT"
    timeframe: str = "1d"
    default_symbols: List[str] = field(
        default_factory=lambda: ["BTC/USDT", "ETH/USDT"]
    )
    use_dynamic_top_n: bool = True   # True 时使用 CoinGecko 市值前 N（已确认需求：前50）
    top_n: int = 50
    start_date_utc: str = "2020-01-01T00:00:00Z"
    initial_capital_usdt: float = 10000.0
    fee_pct: float = 0.001        # 单边手续费，0.1%
    slippage_pct: float = 0.0005  # 单边滑点估算
    risk_per_trade_pct: float = 0.01    # 每笔交易风险占账户权益比例
    max_position_pct: float = 0.25      # 单资产最大名义仓位占账户权益比例
    allow_short: bool = False     # 不做空，仅现货多头


CRYPTO_CONFIG = CryptoConfig()


# ------------------------------------------------------------------
# 美股模块配置
# ------------------------------------------------------------------

@dataclass
class USStockConfig:
    wikipedia_sp500_url: str = (
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    )
    top_n: int = 20
    start_date: str = "2020-01-01"
    initial_capital_usd: float = 10000.0
    fee_pct: float = 0.0           # 假设无佣金经纪商
    slippage_pct: float = 0.0005
    risk_per_trade_pct: float = 0.01
    max_position_pct: float = 0.25
    allow_short: bool = False


US_STOCK_CONFIG = USStockConfig()


# ------------------------------------------------------------------
# ARK 每日持仓模块配置
# ------------------------------------------------------------------

@dataclass
class ARKConfig:
    # ARK 官方 CSV 链接按基金代码配置化，链接结构若失效需人工更新此处
    # 并在 README 中说明如何核实最新官方地址。
    fund_csv_urls: dict = field(default_factory=lambda: {
        "ARKK": "https://ark-funds.com/wp-content/fundsiteliterature/csv/ARK_INNOVATION_ETF_ARKK_HOLDINGS.csv",
        "ARKW": "https://ark-funds.com/wp-content/fundsiteliterature/csv/ARK_NEXT_GENERATION_INTERNET_ETF_ARKW_HOLDINGS.csv",
        "ARKQ": "https://ark-funds.com/wp-content/fundsiteliterature/csv/ARK_AUTONOMOUS_TECH._%26_ROBOTICS_ETF_ARKQ_HOLDINGS.csv",
        "ARKF": "https://ark-funds.com/wp-content/fundsiteliterature/csv/ARK_FINTECH_INNOVATION_ETF_ARKF_HOLDINGS.csv",
        "ARKG": "https://ark-funds.com/wp-content/fundsiteliterature/csv/ARK_GENOMIC_REVOLUTION_ETF_ARKG_HOLDINGS.csv",
        "ARKX": "https://ark-funds.com/wp-content/fundsiteliterature/csv/ARK_SPACE_EXPLORATION_%26_INNOVATION_ETF_ARKX_HOLDINGS.csv",
    })


ARK_CONFIG = ARKConfig()


# ------------------------------------------------------------------
# SEC 13F 模块配置
# ------------------------------------------------------------------

@dataclass
class SEC13FConfig:
    # 默认列表为空。用户需自行从 SEC EDGAR 官方全文检索
    # (https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany)
    # 确认目标机构的真实 CIK 后手动填入，不得编造。
    target_ciks: dict = field(default_factory=lambda: {
        # 示例格式（已注释，默认不启用任何机构）：
        # "BERKSHIRE_HATHAWAY": "0001067983",
    })
    edgar_submissions_url_template: str = (
        "https://data.sec.gov/submissions/CIK{cik}.json"
    )
    edgar_full_text_search_url: str = (
        "https://www.sec.gov/cgi-bin/browse-edgar"
    )


SEC_13F_CONFIG = SEC13FConfig()


# ------------------------------------------------------------------
# 策略与指标配置
# ------------------------------------------------------------------

@dataclass
class StrategyConfig:
    ema_fast: int = 20
    ema_slow: int = 50
    atr_window: int = 14
    adx_window: int = 14
    rsi_window: int = 14
    bollinger_window: int = 20
    bollinger_std: float = 2.0

    adx_trend_threshold: float = 25.0

    atr_stop_loss_mult: float = 2.0
    atr_take_profit_mult: float = 3.0
    atr_trailing_stop_mult: float = 1.5

    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    rsi_exit_recovery: float = 50.0

    max_holding_days: int = 60  # 超时强制平仓的兜底天数，防止信号无限期悬挂


STRATEGY_CONFIG = StrategyConfig()

RISK_FREE_RATE_ANNUAL = 0.0  # Sharpe计算用的无风险利率假设，默认0，README需注明

# ------------------------------------------------------------------
# 运行时间说明（写入README的依据）
# ------------------------------------------------------------------
# GitHub Actions 定时运行固定设置为 UTC 13:00，不随美股夏令时调整，
# 实际对应的美东时间会在冬令时/夏令时之间有1小时差异。
SCHEDULED_RUN_UTC_HOUR = 13
