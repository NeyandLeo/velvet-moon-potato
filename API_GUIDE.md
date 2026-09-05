> 历史探索笔记，并非当前 API 接入方案。当前程序只监听页面自身排片响应，不复制签名或直接调用下单接口，详见 [README.md](README.md)。

"""
猫眼 API 分析和调整指南
======================

本文档帮助你理解如何通过抓包分析猫眼的真实 API，并调整代码以提高抢票成功率。

## 第一步：抓包分析猫眼 API

### 1.1 使用浏览器开发者工具

1. 打开 Chrome 浏览器
2. 访问 https://www.maoyan.com
3. 按 F12 打开开发者工具
4. 切换到 "Network" (网络) 标签
5. 勾选 "Preserve log" (保留日志)
6. 执行以下操作并观察请求：
   - 搜索电影 "奥德赛"
   - 选择影城
   - 查看场次列表
   - 点击购买
   - 选座

### 1.2 关键 API 接口（需要实际抓包确认）

根据猫眼的常见结构，可能存在以下 API：

```
# 搜索电影
GET https://m.maoyan.com/ajax/search?kw=奥德赛

# 获取电影详情
GET https://m.maoyan.com/ajax/movieOnInfoList?movieId=12345

# 获取影城列表
GET https://m.maoyan.com/ajax/cinemaList?movieId=12345&day=2026-09-05

# 获取场次信息
GET https://m.maoyan.com/ajax/showList?cinemaId=6789&movieId=12345

# 创建订单
POST https://m.maoyan.com/ajax/order/create
Body: {
  "showId": "xxx",
  "seatIds": ["A1", "A2"],
  "channelId": "xxx"
}

# 获取座位图
GET https://m.maoyan.com/ajax/seatMap?showId=xxx
```

### 1.3 重要参数

抓包时需要关注：
- **请求头**：Cookie, User-Agent, Referer, X-Requested-With
- **必需参数**：token, sign, timestamp 等防刷参数
- **响应格式**：data 字段结构、错误码含义
- **加密方式**：是否有参数签名、加密算法

---

## 第二步：使用 API 而非浏览器自动化

### 2.1 为什么要用 API？

| 方式 | 优点 | 缺点 |
|------|------|------|
| **Playwright (当前)** | 简单、可视化、无需分析协议 | 慢、易检测、资源占用大 |
| **直接调用 API** | 快（毫秒级）、轻量、难检测 | 需要逆向分析、可能有反爬 |

**结论**：对于秒杀场景，API 方式速度优势明显。

### 2.2 API 调用示例代码

假设你已经抓包分析出了 API，可以这样改造：

```python
import requests
import time
import hashlib
from datetime import datetime

class MaoyanAPIGrabber:
    """基于 API 的抢票器（需要先抓包分析）"""
    
    def __init__(self):
        self.session = requests.Session()
        self.base_url = "https://m.maoyan.com"
        
        # 从浏览器复制你的 Cookie
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)',
            'Referer': 'https://m.maoyan.com/',
            'Cookie': 'YOUR_COOKIE_HERE',  # 重要！需要登录后的 Cookie
            'X-Requested-With': 'XMLHttpRequest'
        })
    
    def search_movie(self, movie_name: str):
        """搜索电影"""
        url = f"{self.base_url}/ajax/search"
        params = {
            'kw': movie_name,
            'cityId': 1,  # 城市 ID（抓包获取）
        }
        
        resp = self.session.get(url, params=params)
        data = resp.json()
        
        if data['status'] == 0:
            movies = data['data']['movies']
            if movies:
                return movies[0]['id']  # 返回第一个匹配的电影 ID
        return None
    
    def get_show_list(self, cinema_id: str, movie_id: str, date: str):
        """获取场次列表"""
        url = f"{self.base_url}/ajax/showList"
        params = {
            'cinemaId': cinema_id,
            'movieId': movie_id,
            'date': date
        }
        
        resp = self.session.get(url, params=params)
        data = resp.json()
        
        if data['status'] == 0:
            return data['data']['shows']  # 返回场次列表
        return []
    
    def get_seat_map(self, show_id: str):
        """获取座位图"""
        url = f"{self.base_url}/ajax/seatMap"
        params = {'showId': show_id}
        
        resp = self.session.get(url, params=params)
        data = resp.json()
        
        return data['data']['seats']
    
    def create_order(self, show_id: str, seat_ids: list):
        """创建订单（关键接口）"""
        url = f"{self.base_url}/ajax/order/create"
        
        # 可能需要签名参数
        timestamp = int(time.time() * 1000)
        sign = self._generate_sign(show_id, seat_ids, timestamp)
        
        payload = {
            'showId': show_id,
            'seatIds': ','.join(seat_ids),
            'timestamp': timestamp,
            'sign': sign,  # 签名（如果有）
            'channelId': 'xxx'  # 渠道 ID（抓包获取）
        }
        
        resp = self.session.post(url, json=payload)
        return resp.json()
    
    def _generate_sign(self, show_id, seat_ids, timestamp):
        """
        生成签名（需要逆向分析 JS 得到算法）
        常见算法：MD5(showId + seatIds + timestamp + secret_key)
        """
        # 这里需要通过分析 JS 代码得到签名逻辑
        # 示例（实际算法需要抓包分析）：
        secret = "MAOYAN_SECRET_KEY"  # 需要从 JS 中提取
        raw = f"{show_id}{','.join(seat_ids)}{timestamp}{secret}"
        return hashlib.md5(raw.encode()).hexdigest()
    
    def fast_grab(self, cinema_id, movie_id, target_time_str, seat_count=2):
        """
        高速抢票主流程
        """
        target_time = datetime.fromisoformat(target_time_str)
        
        print(f"目标时间: {target_time}，开始监控...")
        
        while datetime.now() < target_time + timedelta(minutes=5):
            # 查询场次
            shows = self.get_show_list(cinema_id, movie_id, target_time.strftime('%Y-%m-%d'))
            
            for show in shows:
                # 检查是否可购买
                if show['status'] == '售票中':
                    show_id = show['id']
                    
                    # 获取座位
                    seats = self.get_seat_map(show_id)
                    available = [s['id'] for s in seats if s['status'] == 'available']
                    
                    if len(available) >= seat_count:
                        # 选择中间座位
                        selected = self._select_center_seats(available, seat_count)
                        
                        # 立即下单
                        result = self.create_order(show_id, selected)
                        
                        if result['status'] == 0:
                            print(f"✅ 抢票成功！订单号: {result['data']['orderId']}")
                            return result
                        else:
                            print(f"❌ 下单失败: {result.get('msg')}")
            
            # 动态轮询间隔
            remaining = (target_time - datetime.now()).total_seconds()
            if remaining < 10:
                time.sleep(0.1)  # API 模式可以更快！
            elif remaining < 60:
                time.sleep(0.5)
            else:
                time.sleep(2)
    
    def _select_center_seats(self, available_seats, count):
        """选择中间座位"""
        middle = len(available_seats) // 2
        start = max(0, middle - count // 2)
        return available_seats[start:start + count]
```

---

## 第三步：处理反爬虫机制

### 3.1 常见的反爬手段

1. **Cookie 验证**
   - 需要登录后的有效 Cookie
   - Cookie 可能有过期时间
   - 解决：定期更新 Cookie，或实现自动登录

2. **请求签名**
   - 参数需要带签名（sign、token 等）
   - 签名算法在前端 JS 中
   - 解决：逆向 JS 得到签名算法

3. **频率限制**
   - 同一 IP/账号请求频率限制
   - 解决：控制请求频率、使用代理池

4. **设备指纹**
   - 通过浏览器指纹识别设备
   - 解决：使用真实浏览器环境（Playwright）或伪造指纹

5. **验证码**
   - 高频请求触发验证码
   - 解决：接入打码平台、降低请求频率

### 3.2 逆向 JS 获取签名算法

**步骤**：

1. 在 Network 中找到关键请求
2. 右键 → "Initiator" 查看调用栈
3. 定位到生成签名的 JS 代码
4. 分析加密逻辑（通常是 MD5、SHA256）
5. 用 Python 实现相同算法

**示例**：假设找到这段 JS
```javascript
function generateSign(params) {
    var str = params.showId + params.timestamp + "maoyan_secret_2024";
    return md5(str);
}
```

Python 实现：
```python
import hashlib

def generate_sign(show_id, timestamp):
    raw = f"{show_id}{timestamp}maoyan_secret_2024"
    return hashlib.md5(raw.encode()).hexdigest()
```

### 3.3 Cookie 管理

```python
class CookieManager:
    """Cookie 管理器"""
    
    def __init__(self):
        self.cookie_file = 'maoyan_cookies.json'
    
    def save_cookies(self, cookies):
        """保存 Cookie"""
        with open(self.cookie_file, 'w') as f:
            json.dump(cookies, f)
    
    def load_cookies(self):
        """加载 Cookie"""
        try:
            with open(self.cookie_file, 'r') as f:
                return json.load(f)
        except:
            return None
    
    def is_expired(self, cookies):
        """检查 Cookie 是否过期"""
        # 实现过期检查逻辑
        # 可以尝试发一个轻量请求验证
        pass
```

---

## 第四步：完整的生产级改造

### 4.1 混合模式：Playwright + API

**最佳实践**：
- 用 Playwright 完成登录、获取 Cookie
- 用 API 完成高速抢票

```python
class HybridGrabber:
    """混合模式抢票器"""
    
    async def init(self):
        # 使用 Playwright 登录
        await self.playwright_login()
        
        # 提取 Cookie 给 requests 使用
        cookies = await self.page.context.cookies()
        self.api_session.cookies.update({c['name']: c['value'] for c in cookies})
    
    async def playwright_login(self):
        """Playwright 完成登录"""
        await self.page.goto('https://passport.maoyan.com/login')
        # 等待用户手动登录
        await self.page.wait_for_url('**/index')
    
    def api_grab(self):
        """API 高速抢票"""
        # 使用 API 模式抢票
        pass
```

### 4.2 多账号并发

```python
import asyncio
from concurrent.futures import ThreadPoolExecutor

class MultiAccountGrabber:
    """多账号并发抢票"""
    
    def __init__(self, accounts):
        self.accounts = accounts  # [{'phone': 'xxx', 'password': 'xxx'}, ...]
        self.executor = ThreadPoolExecutor(max_workers=len(accounts))
    
    def grab_with_all_accounts(self, config):
        """所有账号同时抢"""
        futures = []
        
        for account in self.accounts:
            future = self.executor.submit(self._grab_single, account, config)
            futures.append(future)
        
        # 任意一个成功即可
        for future in futures:
            result = future.result()
            if result['success']:
                print(f"✅ 账号 {result['account']} 抢票成功！")
                return result
        
        return {'success': False, 'msg': '所有账号均失败'}
    
    def _grab_single(self, account, config):
        """单账号抢票"""
        grabber = MaoyanAPIGrabber()
        # 登录
        grabber.login(account['phone'], account['password'])
        # 抢票
        return grabber.fast_grab(**config)
```

### 4.3 通知功能

```python
import smtplib
from email.mime.text import MIMEText

class Notifier:
    """抢票结果通知"""
    
    def notify_success(self, order_info):
        """成功通知"""
        # 邮件通知
        self.send_email(f"抢票成功！订单号: {order_info['orderId']}")
        
        # 微信推送（可接入 Server酱 等）
        self.send_wechat(f"已抢到票，请尽快支付")
    
    def send_email(self, content):
        """发送邮件"""
        msg = MIMEText(content)
        msg['Subject'] = '猫眼抢票结果'
        msg['From'] = 'your_email@example.com'
        msg['To'] = 'your_email@example.com'
        
        # 发送邮件逻辑
        pass
```

### 4.4 日志和监控

```python
import logging
from datetime import datetime

class GrabberMonitor:
    """抢票监控"""
    
    def __init__(self):
        logging.basicConfig(
            filename=f'grabber_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log',
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)
        
        self.metrics = {
            'requests_count': 0,
            'success_count': 0,
            'error_count': 0,
            'start_time': None
        }
    
    def log_request(self, url, status_code, response_time):
        """记录每次请求"""
        self.metrics['requests_count'] += 1
        self.logger.info(f"{url} - {status_code} - {response_time}ms")
    
    def log_success(self, order_id):
        """记录成功"""
        self.metrics['success_count'] += 1
        self.logger.info(f"SUCCESS - Order: {order_id}")
    
    def generate_report(self):
        """生成报告"""
        duration = datetime.now() - self.metrics['start_time']
        return f"""
        抢票报告
        ========
        运行时长: {duration}
        请求次数: {self.metrics['requests_count']}
        成功次数: {self.metrics['success_count']}
        失败次数: {self.metrics['error_count']}
        """
```

---

## 第五步：实际工作清单

### ✅ 必须完成的任务

1. **[ ] 抓包分析**
   - 使用 Chrome DevTools 抓取所有关键 API
   - 记录请求头、参数、响应格式
   - 找出必需的认证参数（Cookie、Token）

2. **[ ] 逆向签名算法**
   - 在 JS 中找到签名生成函数
   - 用 Python 实现相同逻辑
   - 验证签名是否正确

3. **[ ] Cookie 管理**
   - 实现自动登录（或手动登录后保存）
   - 检测 Cookie 过期并刷新
   - 支持多账号 Cookie 池

4. **[ ] API 封装**
   - 封装搜索、查询场次、获取座位、下单等 API
   - 统一错误处理
   - 添加重试机制

5. **[ ] 选座策略优化**
   - 根据座位图数据结构调整算法
   - 考虑视角、距离等因素
   - 支持自定义选座偏好

6. **[ ] 测试验证**
   - 非高峰时段测试完整流程
   - 验证每个 API 是否正常
   - 检查是否触发风控

### 🔧 可选优化

7. **[ ] 多账号并发**
   - 提高成功率
   - 实现账号池管理

8. **[ ] 代理池**
   - 避免 IP 被封
   - 提高并发能力

9. **[ ] 验证码识别**
   - 接入打码平台（若需要）

10. **[ ] 通知系统**
    - 邮件/微信推送
    - 实时监控大屏

11. **[ ] 性能优化**
    - 使用异步请求
    - 连接池复用
    - 缓存常用数据

---

## 第六步：风险和注意事项

### ⚠️ 法律风险
- 自动化抢票可能违反平台服务条款
- 商业化使用可能涉及法律问题
- 仅供个人学习使用

### ⚠️ 技术风险
- 账号可能被封禁
- IP 可能被拉黑
- 订单可能被取消

### ⚠️ 道德考量
- 可能影响其他用户的公平购票机会
- 建议仅用于学习和研究

---

## 总结

### 当前工具（Playwright）适用场景：
- ✅ 快速原型开发
- ✅ 无需分析协议
- ✅ 可视化调试
- ❌ 速度较慢
- ❌ 易被检测

### API 模式适用场景：
- ✅ 毫秒级响应
- ✅ 轻量资源占用
- ✅ 难以检测
- ❌ 需要逆向分析
- ❌ 维护成本高

### 建议的最终方案：
**混合模式** = Playwright（登录、Cookie） + API（抢票核心）

这样兼具两者优势，既能快速获取认证信息，又能在关键时刻达到最高速度。
"""
