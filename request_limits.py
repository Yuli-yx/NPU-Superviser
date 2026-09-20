"""单进程内网服务的限流；真实 peer IP，不信任客户端伪造代理头。"""
import math
import threading
import time
from collections import OrderedDict, deque


class RequestLimiter:
    def __init__(self, clock=time.monotonic, max_keys=4096):
        self.clock = clock
        self.max_keys = max_keys
        self.lock = threading.Lock()
        self.events = OrderedDict()

    def check(self, policies):
        """多个窗口原子检查；被拒绝的请求不延长惩罚时间。"""
        now = self.clock()
        with self.lock:
            stale = [key for key, events in self.events.items() if not events or now - events[-1] >= 60]
            for key in stale:
                del self.events[key]
            prepared = []
            retry = 0
            for key, count, seconds in policies:
                events = self.events.get(key, deque())
                while events and now - events[0] >= seconds:
                    events.popleft()
                if len(events) >= count:
                    retry = max(retry, math.ceil(seconds - (now - events[0])))
                prepared.append((key, events))
            if retry:
                return max(1, retry)
            for key, events in prepared:
                events.append(now)
                self.events[key] = events
                self.events.move_to_end(key)
            while len(self.events) > self.max_keys:
                self.events.popitem(last=False)
        return 0

    def reset(self):
        with self.lock:
            self.events.clear()
