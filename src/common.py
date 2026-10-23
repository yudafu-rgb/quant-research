"""
common.py
通用工具模块：日志、HTTP请求重试、时间处理、运行ID生成。
"""

import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import requests

from src.config import (
    LOG_DIR,
    HTTP_TIMEOUT_SECONDS,
    HTTP_MAX_RETRIES,
    HTTP_BACKOFF_BASE_SECONDS,
)

_loggers = {}


def get_logger(name: str) -> logging.Logger:
    """
    获取（或创建）一个同时输出到控制台和按日期命名日志文件的logger。
    同一个name重复调用会返回同一个logger实例，避免重复添加handler。
    """
    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_file = os.path.join(LOG_DIR, f"run_{date_str}.log")
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    _loggers[name] = logger
    return logger


def utc_now_iso() -> str:
    """返回当前UTC时间的ISO 8601字符串表示，例如 2024-01-01T12:00:00+00:00。"""
    return datetime.now(timezone.utc).isoformat()


def generate_run_id() -> str:
    """
    生成一个全局唯一的运行ID，格式：run_{UTC时间戳}_{短随机UUID}。
    用于数据库runs表主键，以及实现各类写入操作的幂等性。
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short_uuid = uuid.uuid4().hex[:8]
    return f"run_{ts}_{short_uuid}"


def http_get_with_retry(
    url: str,
    params: dict = None,
    headers: dict = None,
    logger: logging.Logger = None,
    timeout: int = HTTP_TIMEOUT_SECONDS,
    max_retries: int = HTTP_MAX_RETRIES,
    backoff_base: float = HTTP_BACKOFF_BASE_SECONDS,
) -> requests.Response:
    """
    带指数退避重试的HTTP GET请求。

    特殊处理：
    - 遇到429（限流）状态码时，优先读取响应头 Retry-After，
      若存在则按该值等待，否则按指数退避等待。
    - 遇到5xx服务器错误时，按指数退避重试。
    - 遇到4xx（非429）客户端错误时，通常代表请求本身有问题
      （如URL错误、参数错误），不会重试，直接抛出异常，
      避免无意义的重复请求掩盖真实错误。
    - 达到最大重试次数后仍失败，抛出最后一次遇到的异常。
    """
    log = logger or get_logger("common")
    last_exception = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                if retry_after is not None:
                    try:
                        wait_s = float(retry_after)
                    except ValueError:
                        wait_s = backoff_base * (2 ** (attempt - 1))
                else:
                    wait_s = backoff_base * (2 ** (attempt - 1))
                log.warning(
                    f"请求被限流(429): {url}，第{attempt}次尝试，"
                    f"{wait_s:.1f}s后重试。"
                )
                time.sleep(wait_s)
                continue

            if 500 <= resp.status_code < 600:
                wait_s = backoff_base * (2 ** (attempt - 1))
                log.warning(
                    f"服务器错误({resp.status_code}): {url}，第{attempt}次尝试，"
                    f"{wait_s:.1f}s后重试。"
                )
                time.sleep(wait_s)
                continue

            if 400 <= resp.status_code < 500:
                resp.raise_for_status()

            resp.raise_for_status()
            return resp

        except requests.exceptions.RequestException as e:
            last_exception = e
            if attempt < max_retries:
                wait_s = backoff_base * (2 ** (attempt - 1))
                log.warning(
                    f"请求异常: {url}，第{attempt}次尝试失败: {e}，"
                    f"{wait_s:.1f}s后重试。"
                )
                time.sleep(wait_s)
            else:
                log.error(f"请求最终失败（已重试{max_retries}次）: {url}，错误: {e}")

    raise last_exception if last_exception else RuntimeError(f"请求失败: {url}")
