"""
频率控制 — 防风控
基础延迟 + 随机长停顿 + 限流后指数退避
"""
import random
import time


class RateController:
    def __init__(self):
        self._backoff = {}       # api_name -> 当前退避级别

    def wait_before(self, api_name: str = "default", fast: bool = False):
        """请求前等待
        fast=True: 用于详情补全等高频操作, 更短延迟
        """
        # 计算基础延迟
        backoff_level = self._backoff.get(api_name, 0)

        if backoff_level > 0:
            # 指数退避: 5s → 10s → 20s → 60s
            delays = [5, 10, 20, 60]
            delay = delays[min(backoff_level - 1, len(delays) - 1)]
            delay *= random.uniform(0.8, 1.2)  # 加点随机
            print(f"  [rate] 退避等待 {delay:.1f}s (level={backoff_level})")
            time.sleep(delay)
            return

        if fast:
            # 快速模式: 详情补全/enrichment (并行场景下每线程独立)
            delay = random.uniform(0.02, 0.08)
            time.sleep(delay)
            return

        # 正常延迟 (列表翻页)
        if random.random() < 0.02:
            # 2% 概率长停顿
            delay = random.uniform(1.0, 2.0)
        else:
            delay = random.uniform(0.2, 0.5)

        time.sleep(delay)

    def report_rate_limit(self, api_name: str = "default"):
        """触发限流后增加退避级别"""
        level = self._backoff.get(api_name, 0)
        self._backoff[api_name] = min(level + 1, 4)
        print(f"  [rate] {api_name} 限流, 退避升级到 level={self._backoff[api_name]}")

    def report_success(self, api_name: str = "default"):
        """成功后逐步降低退避"""
        if api_name in self._backoff and self._backoff[api_name] > 0:
            self._backoff[api_name] = max(0, self._backoff[api_name] - 1)

    def reset(self, api_name: str = None):
        """重置退避"""
        if api_name:
            self._backoff.pop(api_name, None)
        else:
            self._backoff.clear()
