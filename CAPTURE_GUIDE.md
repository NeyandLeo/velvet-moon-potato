> 历史探索笔记。当前流程见 [README.md](README.md)；不要上传完整 Cookie、Token、手机号或未经脱敏的录制数据。

"""
实战抓包步骤指南
================

手把手教你如何抓取猫眼的真实 API 并应用到代码中。

## 准备工作

### 工具清单
- ✅ Chrome 浏览器
- ✅ 猫眼账号（已登录）
- ✅ 纸笔或文本编辑器（记录 API）

---

## 第一步：打开开发者工具

1. 打开 Chrome 浏览器
2. 访问 https://www.maoyan.com
3. 按 **F12** 或 **右键 → 检查**
4. 切换到 **Network（网络）** 标签
5. 勾选 **Preserve log（保留日志）**
6. 点击 **Clear（清空）** 清除现有记录

---

## 第二步：搜索电影

### 操作
1. 在猫眼首页搜索框输入 "奥德赛"
2. 按回车或点击搜索

### 抓包分析

在 Network 中找到类似这样的请求：
```
Name: search?kw=奥德赛
Type: XHR
Status: 200
```

#### 点击该请求，查看详情：

**General（基本信息）：**
```
Request URL: https://m.maoyan.com/ajax/search?kw=奥德赛&cityId=1
Request Method: GET
Status Code: 200 OK
```

**Request Headers（请求头）：**
```
Cookie: uuid_n_v=v1_xxx; ci=1; __mta=xxx; ...
User-Agent: Mozilla/5.0 ...
Referer: https://m.maoyan.com/
```

**Response（响应）：**
```json
{
  "status": 0,
  "data": {
    "movies": [
      {
        "id": 1458431,
        "nm": "奥德赛",
        "img": "https://...",
        "cat": "剧情,科幻"
      }
    ]
  }
}
```

### 记录关键信息

创建文件 `api_endpoints.txt`：
```
=== 搜索电影 ===
URL: https://m.maoyan.com/ajax/search
Method: GET
Params:
  - kw: 电影名
  - cityId: 城市ID（例如：1=北京）
Response:
  - data.movies[].id: 电影ID
  - data.movies[].nm: 电影名称
```

---

## 第三步：选择影城

### 操作
1. 点击搜索结果中的电影
2. 选择 "购票" 标签
3. 观察影城列表加载

### 抓包分析

找到影城列表请求：
```
Name: cinemaList?movieId=1458431
Type: XHR
```

**Request URL：**
```
https://m.maoyan.com/ajax/cinemaList?movieId=1458431&day=2026-09-05&offset=0&limit=20
```

**Response：**
```json
{
  "status": 0,
  "data": {
    "cinemas": [
      {
        "id": 2123,
        "nm": "万达影城(CBD店)",
        "addr": "朝阳区建国路xxx",
        "distance": "2.5km"
      }
    ]
  }
}
```

### 更新记录

```
=== 影城列表 ===
URL: https://m.maoyan.com/ajax/cinemaList
Method: GET
Params:
  - movieId: 电影ID
  - day: 日期 (YYYY-MM-DD)
  - offset: 分页偏移
  - limit: 每页数量
Response:
  - data.cinemas[].id: 影城ID
  - data.cinemas[].nm: 影城名称
```

---

## 第四步：获取场次信息

### 操作
1. 点击某个影城
2. 查看该影城的场次列表

### 抓包分析

找到场次列表请求：
```
Name: showList?cinemaId=2123&movieId=1458431
Type: XHR
```

**Request URL：**
```
https://m.maoyan.com/ajax/showList?cinemaId=2123&movieId=1458431
```

**Response：**
```json
{
  "status": 0,
  "data": {
    "shows": [
      {
        "id": "234567890",
        "tm": "19:30",
        "lang": "国语",
        "tp": "2D",
        "hallName": "1号厅(IMAX)",
        "price": 68.00,
        "saleFlag": "Sale"  // Sale=可购买, SoldOut=售罄, PreSale=预售
      }
    ]
  }
}
```

### 更新记录

```
=== 场次列表 ===
URL: https://m.maoyan.com/ajax/showList
Method: GET
Params:
  - cinemaId: 影城ID
  - movieId: 电影ID
Response:
  - data.shows[].id: 场次ID
  - data.shows[].tm: 时间
  - data.shows[].hallName: 影厅名称
  - data.shows[].saleFlag: 售卖状态
```

---

## 第五步：选座界面

### 操作
1. 点击某个可购买的场次
2. 进入选座页面

### 抓包分析

找到座位图请求：
```
Name: seatMap?showId=234567890
Type: XHR
```

**Request URL：**
```
https://m.maoyan.com/ajax/seatMap?showId=234567890&token=abc123
```

**注意**：可能需要 token！

**Response：**
```json
{
  "status": 0,
  "data": {
    "seatData": {
      "sections": [
        {
          "sectionId": "1",
          "sectionName": "普通区",
          "seats": [
            {
              "seatId": "1_5_8",  // 区域_排_列
              "rowId": "5",
              "columnId": "8",
              "st": 1  // 1=可选, 2=已售, 3=锁定
            }
          ]
        }
      ]
    }
  }
}
```

### 更新记录

```
=== 座位图 ===
URL: https://m.maoyan.com/ajax/seatMap
Method: GET
Params:
  - showId: 场次ID
  - token: 令牌（可能需要）
Response:
  - data.seatData.sections[].seats[].seatId: 座位ID
  - data.seatData.sections[].seats[].st: 状态
```

---

## 第六步：创建订单（最关键！）

### 操作
1. 在座位图上选择座位
2. 点击 "确认选座"
3. **注意**：这一步会真的生成订单，谨慎操作！

### 抓包分析

找到订单创建请求：
```
Name: order/create
Type: XHR
Method: POST
```

**Request URL：**
```
https://m.maoyan.com/ajax/order/create
```

**Request Headers：**
```
Content-Type: application/json
Cookie: (必需登录 Cookie)
```

**Request Payload（POST 数据）：**
```json
{
  "showId": "234567890",
  "seatIds": "1_5_8,1_5_9",
  "channelId": "1",
  "timestamp": 1725436800000,
  "sign": "a1b2c3d4e5f6..."  // ⚠️ 签名！需要逆向
}
```

**Response：**
```json
{
  "status": 0,
  "data": {
    "orderId": "O2026090412345678",
    "totalPrice": 136.00,
    "payUrl": "https://..."
  }
}
```

### 关键发现

🔴 **注意签名参数！**
- `sign` 参数是防刷的关键
- 需要找到生成签名的 JS 代码
- 下一步会教你如何逆向

### 更新记录

```
=== 创建订单 ===
URL: https://m.maoyan.com/ajax/order/create
Method: POST
Headers:
  - Content-Type: application/json
  - Cookie: (必需)
Body:
  - showId: 场次ID
  - seatIds: 座位ID列表（逗号分隔）
  - channelId: 渠道ID
  - timestamp: 时间戳
  - sign: 签名 ⚠️
Response:
  - data.orderId: 订单ID
```

---

## 第七步：逆向签名算法

### 找到签名代码

1. 在订单创建请求上右键
2. 选择 **Initiator（发起者）** 标签
3. 点击调用栈中的 JS 文件
4. 查找 `sign` 或 `generateSign` 关键词

### 常见签名算法

#### 示例 1：MD5 签名
```javascript
// 找到的 JS 代码
function generateSign(params) {
    var str = params.showId + params.seatIds + params.timestamp + "maoyan2024";
    return md5(str);
}
```

**Python 实现：**
```python
import hashlib

def generate_sign(show_id, seat_ids, timestamp):
    raw = f"{show_id}{seat_ids}{timestamp}maoyan2024"
    return hashlib.md5(raw.encode()).hexdigest()
```

#### 示例 2：排序后签名
```javascript
// 找到的 JS 代码
function generateSign(params) {
    var keys = Object.keys(params).sort();
    var str = "";
    keys.forEach(function(key) {
        str += key + "=" + params[key] + "&";
    });
    str += "secret=maoyan_key_2024";
    return md5(str);
}
```

**Python 实现：**
```python
def generate_sign(params):
    sorted_keys = sorted(params.keys())
    raw = ''.join([f"{k}={params[k]}&" for k in sorted_keys])
    raw += "secret=maoyan_key_2024"
    return hashlib.md5(raw.encode()).hexdigest()
```

---

## 第八步：验证和测试

### 使用测试工具

```bash
python api_tester.py
```

### 修改测试器

编辑 `api_tester.py`，添加真实 Cookie：
```python
self.cookie = "uuid_n_v=xxx; ci=1; __mta=xxx; ..."  # 从浏览器复制
```

### 测试每个接口

```python
# 测试搜索
tester.test_search("奥德赛")

# 测试影城列表（使用真实电影ID）
tester.test_cinema_list(movie_id="1458431")

# 测试场次列表（使用真实影城ID和电影ID）
tester.test_show_list(cinema_id="2123", movie_id="1458431")
```

---

## 第九步：更新代码

### 修改 app.py

根据抓包结果，更新 API 调用：

```python
async def search_movie(self, movie_name: str) -> Optional[str]:
    """搜索电影 - 使用真实 API"""
    url = "https://m.maoyan.com/ajax/search"
    params = {
        'kw': movie_name,
        'cityId': 1  # 从抓包获取
    }
    
    # 使用 requests 替代浏览器
    resp = requests.get(url, params=params, headers=self.headers)
    data = resp.json()
    
    if data['status'] == 0:
        movies = data['data']['movies']
        if movies:
            return movies[0]['id']
    
    return None
```

### 完整替换流程

将所有 Playwright 操作替换为 API 调用：

```python
class MaoyanAPIGrabber:
    def __init__(self):
        self.session = requests.Session()
        # ... 设置 headers 和 cookies
    
    def search_movie(self, name):
        # API 调用
        pass
    
    def get_cinemas(self, movie_id, date):
        # API 调用
        pass
    
    def get_shows(self, cinema_id, movie_id):
        # API 调用
        pass
    
    def get_seat_map(self, show_id):
        # API 调用
        pass
    
    def create_order(self, show_id, seat_ids):
        # API 调用 + 签名
        pass
```

---

## 第十步：性能对比测试

### Playwright 模式（当前）
```
搜索电影: 3-5 秒
选择影城: 2-3 秒
查看场次: 2-3 秒
选座: 2-4 秒
提交订单: 1-2 秒
总计: 10-17 秒
```

### API 模式（优化后）
```
搜索电影: 0.1-0.2 秒
选择影城: 0.1-0.2 秒
查看场次: 0.1-0.2 秒
选座: 0.05 秒
提交订单: 0.2-0.3 秒
总计: 0.5-1 秒
```

**速度提升：10-30 倍！**

---

## 常见问题

### Q1: 抓不到请求怎么办？
**A:** 
- 确保勾选了 "Preserve log"
- 尝试清空后重新操作
- 检查是否在正确的 Network 标签
- 尝试使用移动端模式（F12 → Toggle device toolbar）

### Q2: Cookie 在哪里复制？
**A:**
1. F12 → Application → Cookies → https://www.maoyan.com
2. 或者在 Network 中的请求 Headers 里找到 Cookie
3. 复制完整的 Cookie 字符串

### Q3: 签名算法太复杂怎么办？
**A:**
- 使用混合模式：Playwright 登录 + 手动触发购买
- 或者使用 Playwright 完成整个流程（慢但稳定）
- 考虑使用 JavaScript 引擎（如 PyExecJS）直接执行 JS

### Q4: 如何处理验证码？
**A:**
- 接入打码平台（如：超级鹰、若快）
- 或者降低请求频率避免触发
- 或者保持长时间登录状态

---

## 总结

完成以上步骤后，你将获得：

✅ 完整的 API 端点列表
✅ 请求和响应的数据结构
✅ 必需的认证参数（Cookie、Token）
✅ 签名算法（如果有）
✅ 高性能的 API 调用代码

下一步：将这些信息应用到 `app.py` 中，实现真正的高速抢票！
"""
