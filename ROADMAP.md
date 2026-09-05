> 历史规划，不是当前验收结果。当前使用说明与限制见 [README.md](README.md)。

"""
从原型到生产：完整路线图
========================

本文档是整个项目的导航指南，告诉你当前有什么、还需要做什么。

## 📦 当前项目状态

### ✅ 已完成

#### 1. 基础框架（Playwright 版本）
- `app.py` - Flask 后端 + Playwright 自动化
- `templates/index.html` - WebUI 界面（深色专业风格）
- 完整的抢票流程：搜索 → 影城 → 场次 → 选座 → 下单

#### 2. 安装和启动脚本
- `install.sh` / `install.bat` - Conda 环境一键安装
- `start.sh` / `start.bat` - 快速启动脚本
- `environment.yml` - Conda 环境配置
- `requirements.txt` - Python 依赖

#### 3. 配置和文档
- `README.md` - 使用说明
- `config.example.json` - 配置示例
- `.gitignore` - Git 忽略文件

#### 4. API 迁移指南
- `API_GUIDE.md` - 详细的 API 改造指南
- `CAPTURE_GUIDE.md` - 手把手抓包教程
- `api_tester.py` - API 测试工具

### ⚠️ 当前限制

1. **速度较慢**：使用浏览器自动化，整个流程需要 10-17 秒
2. **易被检测**：浏览器特征明显
3. **资源占用大**：需要启动完整的浏览器进程
4. **不支持并发**：同时只能运行一个任务

### ✨ 适用场景

当前版本适合：
- ✅ 快速验证流程
- ✅ 学习自动化原理
- ✅ 小规模个人使用
- ✅ 非高峰时段抢票

---

## 🎯 实际工作清单

如果你要让这个工具真正能在放票时刻抢到票，按优先级需要做：

### 🔥 P0 - 必须完成（核心功能）

#### 1. 抓包分析猫眼 API
**目标**：获取真实的 API 端点和参数

**步骤**：
1. 阅读 [`CAPTURE_GUIDE.md`](./CAPTURE_GUIDE.md)
2. 使用 Chrome DevTools 抓取所有关键接口
3. 记录到 `api_endpoints.txt`：
   ```
   搜索电影: GET /ajax/search?kw={name}
   影城列表: GET /ajax/cinemaList?movieId={id}
   场次列表: GET /ajax/showList?cinemaId={cid}&movieId={mid}
   座位图: GET /ajax/seatMap?showId={sid}
   创建订单: POST /ajax/order/create
   ```

**预计耗时**：2-4 小时

---

#### 2. 逆向签名算法（如果有）
**目标**：破解请求签名，才能调用 API

**步骤**：
1. 在 Network 中找到 `order/create` 请求
2. 查看请求参数中是否有 `sign`、`token` 等
3. 右键请求 → Initiator → 找到生成签名的 JS 代码
4. 分析算法（常见：MD5、SHA256）
5. 用 Python 实现相同逻辑

**示例**：
```python
import hashlib

def generate_sign(show_id, seat_ids, timestamp):
    # 根据逆向结果实现
    raw = f"{show_id}{seat_ids}{timestamp}SECRET_KEY"
    return hashlib.md5(raw.encode()).hexdigest()
```

**预计耗时**：2-8 小时（取决于复杂度）

---

#### 3. 改造为 API 模式
**目标**：用 requests 替代 Playwright，速度提升 10-30 倍

**步骤**：
1. 创建新文件 `api_grabber.py`（基于 API_GUIDE.md 的示例）
2. 实现所有核心方法：
   ```python
   class MaoyanAPIGrabber:
       def search_movie(self, name) -> str  # 返回电影ID
       def get_shows(self, cinema_id, movie_id) -> List[Dict]
       def get_seat_map(self, show_id) -> List[str]  # 返回可选座位
       def create_order(self, show_id, seat_ids) -> Dict
   ```
3. 在 `app.py` 中集成 API 模式
4. 添加配置选项：`use_api_mode: true/false`

**预计耗时**：4-8 小时

---

#### 4. Cookie 管理
**目标**：维持登录状态，避免频繁登录

**步骤**：
1. 使用 Playwright 完成登录，提取 Cookie
2. 保存 Cookie 到文件（加密存储）
3. 检测 Cookie 过期并自动刷新
4. 支持多账号 Cookie 池

**代码框架**：
```python
class CookieManager:
    def login_and_save(self):
        # Playwright 登录
        # 提取 Cookie
        # 保存到 cookies.json
        pass
    
    def load_cookies(self):
        # 从文件加载
        pass
    
    def is_valid(self, cookies) -> bool:
        # 验证 Cookie 是否有效
        pass
```

**预计耗时**：2-4 小时

---

### ⭐ P1 - 强烈推荐（提高成功率）

#### 5. 多账号并发
**目标**：同时用多个账号抢同一场次，提高成功率

**步骤**：
1. 支持配置多个账号
2. 每个账号独立 Cookie 和 Session
3. 使用线程池并发执行
4. 任意一个成功即停止

**代码框架**：
```python
from concurrent.futures import ThreadPoolExecutor

class MultiAccountGrabber:
    def __init__(self, accounts: List[Dict]):
        self.accounts = accounts
        self.executor = ThreadPoolExecutor(max_workers=len(accounts))
    
    def grab_parallel(self, config):
        futures = []
        for account in self.accounts:
            future = self.executor.submit(self._single_grab, account, config)
            futures.append(future)
        
        # 返回第一个成功的结果
        for future in futures:
            result = future.result()
            if result['success']:
                return result
```

**预计耗时**：3-5 小时

---

#### 6. 智能重试机制
**目标**：网络波动时自动重试，不错过机会

**步骤**：
```python
import time
from functools import wraps

def retry(max_attempts=3, delay=0.5):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for i in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if i == max_attempts - 1:
                        raise
                    time.sleep(delay)
            return None
        return wrapper
    return decorator

@retry(max_attempts=3)
def create_order(self, show_id, seat_ids):
    # 下单逻辑
    pass
```

**预计耗时**：1-2 小时

---

#### 7. 更好的选座策略
**目标**：根据视角、距离等因素优化选座

**步骤**：
1. 分析座位图数据结构（排数、列数、类型）
2. 实现多种策略：
   - 中间偏后（最佳视角）
   - 黄金比例（0.618 位置）
   - 情侣座优先
   - 避开第一排和最后排
3. 支持用户自定义偏好

**代码框架**：
```python
def select_best_seats(seats, count, preference):
    """
    preference = {
        'position': 'center',  # center, front, back
        'min_row': 5,  # 最小排数
        'max_row': 15,  # 最大排数
        'avoid_edge': True  # 避开边缘
    }
    """
    # 过滤座位
    filtered = [s for s in seats if preference['min_row'] <= s['row'] <= preference['max_row']]
    
    # 计算每个座位的得分
    scored = [(s, calculate_score(s, preference)) for s in filtered]
    
    # 排序并选择最佳的连续座位
    scored.sort(key=lambda x: x[1], reverse=True)
    return find_consecutive_seats(scored, count)
```

**预计耗时**：2-3 小时

---

### 🎨 P2 - 锦上添花（用户体验）

#### 8. 通知系统
**目标**：抢票结果实时推送到手机

**实现方式**：
- **邮件通知**：SMTP 发送邮件
- **微信推送**：接入 Server酱 / pushplus
- **钉钉/飞书**：Webhook 通知
- **短信通知**：接入阿里云/腾讯云 SMS

**代码示例**：
```python
import requests

def send_wechat_notification(title, content):
    """使用 Server酱 推送"""
    url = "https://sctapi.ftqq.com/YOUR_KEY.send"
    data = {
        'title': title,
        'desp': content
    }
    requests.post(url, data=data)

# 使用
send_wechat_notification(
    "抢票成功！", 
    f"已成功抢到《奥德赛》19:30场次，订单号：{order_id}"
)
```

**预计耗时**：2-3 小时

---

#### 9. 更好的日志和监控
**目标**：详细记录每次运行，方便调试和优化

**步骤**：
```python
import logging
from datetime import datetime

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f'logs/grabber_{datetime.now():%Y%m%d_%H%M%S}.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# 记录关键指标
class Metrics:
    def __init__(self):
        self.requests_count = 0
        self.success_count = 0
        self.error_count = 0
        self.response_times = []
    
    def record_request(self, url, response_time, success):
        self.requests_count += 1
        self.response_times.append(response_time)
        if success:
            self.success_count += 1
        else:
            self.error_count += 1
        
        logger.info(f"{url} - {response_time}ms - {'SUCCESS' if success else 'FAILED'}")
```

**预计耗时**：2-3 小时

---

#### 10. 支持定时任务
**目标**：提前设定好，到点自动开始

**步骤**：
```python
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime

scheduler = BackgroundScheduler()

def schedule_grab(config, start_time):
    """
    config: 抢票配置
    start_time: 开始时间 (datetime 对象)
    """
    scheduler.add_job(
        func=lambda: run_grab_task(config),
        trigger='date',
        run_date=start_time,
        id=f'grab_{start_time.timestamp()}'
    )
    
    print(f"已安排任务，将在 {start_time} 开始执行")

# 使用
schedule_grab(
    config={'movie': '奥德赛', 'cinema': '万达影城'},
    start_time=datetime(2026, 9, 5, 12, 0, 0)  # 2026-09-05 12:00:00
)

scheduler.start()
```

**预计耗时**：1-2 小时

---

### 🔒 P3 - 风险控制（可选但重要）

#### 11. 代理池
**目标**：避免 IP 被封

**实现**：
```python
import random

class ProxyPool:
    def __init__(self, proxies):
        self.proxies = proxies  # ['http://ip1:port', 'http://ip2:port', ...]
    
    def get_random(self):
        return random.choice(self.proxies)
    
    def remove_invalid(self, proxy):
        if proxy in self.proxies:
            self.proxies.remove(proxy)

# 使用
proxy_pool = ProxyPool([...])
proxies = {'http': proxy_pool.get_random(), 'https': proxy_pool.get_random()}
response = requests.get(url, proxies=proxies)
```

**预计耗时**：2-3 小时

---

#### 12. 请求频率控制
**目标**：避免被识别为机器人

**实现**：
```python
import time
import random

class RateLimiter:
    def __init__(self, min_interval=0.5, max_interval=2.0):
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.last_request_time = 0
    
    def wait(self):
        now = time.time()
        elapsed = now - self.last_request_time
        
        if elapsed < self.min_interval:
            sleep_time = random.uniform(self.min_interval, self.max_interval)
            time.sleep(sleep_time)
        
        self.last_request_time = time.time()

# 使用
limiter = RateLimiter(min_interval=0.5, max_interval=2.0)

for _ in range(10):
    limiter.wait()
    response = requests.get(url)
```

**预计耗时**：1-2 小时

---

## 📊 开发优先级总结

### 阶段 1：基础可用（总计 10-24 小时）
1. ✅ 抓包分析 API（2-4h）
2. ✅ 逆向签名算法（2-8h）
3. ✅ 改造为 API 模式（4-8h）
4. ✅ Cookie 管理（2-4h）

**完成后效果**：速度提升 10-30 倍，基本可用

---

### 阶段 2：提高成功率（总计 6-10 小时）
5. ✅ 多账号并发（3-5h）
6. ✅ 智能重试（1-2h）
7. ✅ 更好的选座（2-3h）

**完成后效果**：成功率显著提升

---

### 阶段 3：完善体验（总计 5-8 小时）
8. ✅ 通知系统（2-3h）
9. ✅ 日志监控（2-3h）
10. ✅ 定时任务（1-2h）

**完成后效果**：使用体验接近商业产品

---

### 阶段 4：风险控制（总计 3-5 小时）
11. ✅ 代理池（2-3h）
12. ✅ 频率控制（1-2h）

**完成后效果**：降低被封风险

---

## 🎓 学习路径

### 如果你是初学者
建议顺序：
1. 先用当前的 Playwright 版本熟悉流程
2. 学习抓包（CAPTURE_GUIDE.md）
3. 学习 API 调用（API_GUIDE.md）
4. 逐步实现优化功能

### 如果你有经验
可以直接：
1. 抓包分析 API（半天）
2. 重写为纯 API 版本（1天）
3. 添加并发和重试（半天）
4. 完成！

---

## ⚡ 快速开始

### 现在就可以用
```bash
# 安装
./install.sh

# 启动
./start.sh

# 访问
http://localhost:5000
```

### 测试 API
```bash
# 先抓包，然后运行测试器
python api_tester.py
```

---

## 📚 文档导航

| 文档 | 用途 |
|------|------|
| [README.md](./README.md) | 快速上手指南 |
| [API_GUIDE.md](./API_GUIDE.md) | API 改造详细教程 |
| [CAPTURE_GUIDE.md](./CAPTURE_GUIDE.md) | 手把手抓包教程 |
| [config.example.json](./config.example.json) | 配置文件示例 |

---

## ❓ FAQ

### Q: 当前版本能直接用吗？
A: 可以，但速度较慢（10-17秒），适合非高峰时段或学习测试。

### Q: 一定要改成 API 模式吗？
A: 不一定，但 API 模式快 10-30 倍，对于秒杀场景几乎是必需的。

### Q: 多账号并发会被封吗？
A: 有风险，建议：
- 控制并发数（2-3个账号）
- 使用真实设备的 Cookie
- 添加随机延时

### Q: 需要什么技术背景？
A: 
- 必需：Python 基础、HTTP 协议
- 推荐：爬虫经验、前端 JS 基础
- 加分：逆向工程经验

### Q: 商业使用合法吗？
A: 本工具仅供学习研究，商业使用请咨询法律顾问。

---

## 🚨 免责声明

- 本工具仅供技术学习和研究使用
- 使用本工具产生的任何后果由使用者自行承担
- 请遵守相关法律法规和平台服务条款
- 不提供任何形式的技术支持和法律咨询

---

## 📞 后续支持

完成基础开发后，建议：
1. 小规模测试（非热门场次）
2. 逐步调整参数
3. 积累经验数据
4. 持续优化策略

祝你抢票成功！🎬
"""
