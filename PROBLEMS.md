# 当前设计的问题分析

> 下文分析针对旧版原型；当前实现和仍未验证的能力见 [README.md](README.md)。
=====================================

## 测试结果

运行 `python app.py` 报错：
```
ModuleNotFoundError: No module named 'flask'
```

这暴露了第一个问题：**依赖未安装**

---

## 主要问题清单

### ❌ 问题 1：我设计的是"假"自动化

**现状**：
我写的代码使用了 Playwright，但代码中有很多**硬编码的选择器**，例如：
```python
search_box = await self.page.query_selector('input[placeholder*="搜"]')
cinema_list = await self.page.query_selector('.cinema-list')
show_items = await self.page.query_selector_all('.show-item')
```

**问题**：
- 这些选择器 `.cinema-list`、`.show-item` 是我**猜测**的，不是真实的
- 我没有访问猫眼网站，所以**不知道真实的 HTML 结构**
- 真实的猫眼页面可能完全不同

**后果**：
```python
# 代码会运行到这里
await self.page.wait_for_selector('.cinema-list', timeout=10000)
# 但永远等不到这个元素，因为它不存在 → 超时失败
```

---

### ❌ 问题 2：流程假设可能错误

**我假设的流程**：
1. 访问 maoyan.com 首页
2. 在首页搜索电影
3. 点击电影进入详情
4. 看到影城列表
5. 点击影城看场次
6. 点击购买进入选座
7. 选座并下单

**实际可能的情况**：
- 猫眼可能需要先选择城市
- 可能需要先登录才能看到购买按钮
- 移动版和桌面版流程可能完全不同
- 搜索可能在单独的页面
- 影城和场次可能在同一个页面

**后果**：整个流程可能在第一步就走不通

---

### ❌ 问题 3：没有处理登录

**代码中的问题**：
```python
await self.page.goto('https://www.maoyan.com')
await self.page.wait_for_timeout(2000)
# 直接开始搜索，没有检查是否需要登录
```

**实际情况**：
- 猫眼购票**必须登录**
- 可能需要手机验证码
- 可能有滑块验证
- Cookie 和 Session 管理

**后果**：到支付环节会失败，因为没有登录态

---

### ❌ 问题 4：异步编程问题

**代码中的严重 bug**：
```python
@app.route('/api/start', methods=['POST'])
def start_task():
    # 这是一个同步函数
    async def run():
        grabber = MaoyanTicketGrabber()
        await grabber.run_task(config)  # 异步函数
    
    asyncio.run(run())  # 在 Flask 线程中运行异步
    # 问题：会阻塞 Flask，无法返回响应
```

**后果**：
- 点击"开始抢票"后，页面会一直转圈
- 直到任务结束才能看到响应
- 无法获取实时日志

---

### ❌ 问题 5：日志系统不工作

**代码问题**：
```python
def log_message(message: str, level: str = 'info'):
    grabber_status['logs'].append(log_entry)  # 添加到全局变量
    logger.info(f'[{level.upper()}] {message}')
```

**问题**：
- `grabber_status` 是全局变量
- 但异步任务在另一个线程/进程中运行
- 前端无法实时获取日志

**后果**：WebUI 上看不到任何运行日志

---

### ❌ 问题 6：选座逻辑过于简化

**代码**：
```python
async def _select_center_seats(self, seats, count: int) -> List:
    middle_index = len(seats) // 2
    start = max(0, middle_index - count // 2)
    
    for i in range(start, min(start + count, len(seats))):
        await seats[i].click()
```

**问题**：
- 座位不是简单的列表，而是二维数组（排x列）
- 需要找连续的座位（同一排）
- 没有考虑座位类型（情侣座、残疾人座）
- 没有考虑视角（太前太后）

---

## 根本问题：缺少真实数据

**我没有做的关键步骤**：

1. ❌ 没有打开浏览器访问猫眼
2. ❌ 没有检查真实的 HTML 结构
3. ❌ 没有抓包看真实的 API
4. ❌ 没有测试登录流程
5. ❌ 没有看座位图的数据格式

**结果**：
- 所有代码都是基于**猜测**
- 这就像蒙着眼睛写代码

---

## 为什么我这么设计？

因为你的要求是"研究怎么用 Web 工具购买"，我理解错了：

**你可能想要的**：
1. 先一起分析猫眼网站的真实结构
2. 找出真正可行的自动化方案
3. 再写针对性的代码

**我做的**：
1. 直接写了一套"通用"的抢票框架
2. 假设猫眼长什么样
3. 提供了改造指南

---

## 正确的开发顺序应该是

### 第 1 步：手动探索（最重要！）

```bash
# 打开浏览器，手动操作一遍完整流程
1. 打开 https://www.maoyan.com
2. 登录账号
3. 搜索"奥德赛"
4. 选择影城
5. 选择场次
6. 进入选座页面
7. 观察每一步的 URL 变化
8. 记录下来
```

### 第 2 步：抓包分析

```bash
# 打开 Chrome DevTools (F12)
1. 切换到 Network 标签
2. 勾选 Preserve log
3. 重复上面的流程
4. 记录每一个请求：
   - URL
   - 请求方法（GET/POST）
   - 请求参数
   - 响应数据
```

### 第 3 步：选择技术方案

根据抓包结果，决定：
- **如果 API 简单**：直接用 requests 调用 API
- **如果需要 JS 渲染**：用 Playwright 或 Selenium
- **如果有复杂加密**：可能需要逆向或用浏览器

### 第 4 步：写针对性代码

基于真实的 HTML 结构和 API，写代码：
```python
# 真实的选择器
cinema_item = '.movie-list > .item'  # 从抓包得知
buy_button = 'button[data-action="buy"]'  # 从 HTML 看到
```

### 第 5 步：小步测试

```python
# 每写一个功能就测试
async def test_search():
    await page.goto('https://www.maoyan.com')
    # 测试：能否找到搜索框？
    search_box = await page.query_selector('input[placeholder="搜索"]')
    assert search_box is not None
    print("✅ 找到搜索框")

# 运行测试
test_search()  # 通过了再写下一步
```

---

## 实际上应该怎么做？

让我们重新来，正确的方式：

### 方案 A：从头开始（推荐）

1. 我先用 Playwright 打开猫眼，截图给你看
2. 你告诉我要抢什么电影、哪个影城
3. 我手动操作一遍，记录真实流程
4. 根据真实情况写代码
5. 逐步测试每个环节

### 方案 B：混合模式（实用）

1. 用 Playwright 打开浏览器
2. 让你手动登录（避免验证码问题）
3. 保存登录态（Cookie）
4. 程序接管后续的搜索、选座、下单
5. 支付环节你手动完成

### 方案 C：API 模式（最快但最难）

1. 你先手动抓包，把 API 发给我
2. 我帮你写 requests 调用代码
3. 处理签名、加密等问题
4. 纯 API 调用，速度最快

---

## 当前代码能救吗？

可以，但需要大改：

### 需要改的地方

1. **修复异步问题**：用后台线程运行任务
2. **去除硬编码选择器**：改成可配置
3. **添加登录逻辑**：支持手动登录或 Cookie 导入
4. **添加调试模式**：每一步截图，方便调试
5. **简化选座**：先实现"点击第一个可选座位"

---

## 我的建议

咱们重新开始，用正确的流程：

1. **我先写一个调试工具**：用 Playwright 打开猫眼，让你手动操作
2. **记录你的操作**：每一步的点击、输入，记录下来
3. **生成可用的代码**：基于真实操作生成自动化脚本
4. **测试验证**：确保每一步都能成功
5. **优化性能**：最后再考虑 API 模式

---

## 现在该怎么办？

你有两个选择：

### 选择 1：修复当前代码（困难，不推荐）
- 我帮你把当前代码改成可运行的
- 但仍然需要你手动抓包提供真实数据
- 时间成本：2-4 小时

### 选择 2：用正确方式重写（推荐）
- 我写一个"录制器"工具
- 你手动操作一遍，它记录下来
- 自动生成针对猫眼的代码
- 时间成本：1-2 小时，但结果可靠

你想选哪个？或者你先告诉我：
1. 你刚才测试时，具体在哪一步失败了？
2. 有错误信息吗？
3. 你有登录猫眼账号吗？

这样我能更准确地帮你解决问题！
