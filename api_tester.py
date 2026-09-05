#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
猫眼 API 调试工具
用于抓包后验证和测试 API 接口
"""
import requests
import json
from datetime import datetime


class MaoyanAPITester:
    """API 测试器"""

    def __init__(self):
        self.session = requests.Session()
        self.base_url = "https://m.maoyan.com"

        # TODO: 从浏览器复制你的 Cookie
        self.cookie = ""

        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15',
            'Referer': 'https://m.maoyan.com/',
            'Accept': 'application/json',
            'X-Requested-With': 'XMLHttpRequest'
        })

        if self.cookie:
            self.session.headers['Cookie'] = self.cookie

    def test_search(self, movie_name="奥德赛"):
        """测试搜索接口"""
        print(f"\n=== 测试搜索: {movie_name} ===")

        # 尝试多个可能的 API 端点
        endpoints = [
            f"/ajax/search?kw={movie_name}",
            f"/api/search?keyword={movie_name}",
            f"/search/api?q={movie_name}",
        ]

        for endpoint in endpoints:
            url = self.base_url + endpoint
            try:
                print(f"尝试: {url}")
                resp = self.session.get(url, timeout=5)
                print(f"状态码: {resp.status_code}")

                if resp.status_code == 200:
                    data = resp.json()
                    print(f"响应: {json.dumps(data, ensure_ascii=False, indent=2)[:500]}")

                    # 分析响应结构
                    if 'data' in data:
                        print("✅ 找到可用接口！")
                        return url, data
                else:
                    print(f"❌ 状态码错误: {resp.status_code}")

            except Exception as e:
                print(f"❌ 请求失败: {e}")

        return None, None

    def test_cinema_list(self, movie_id="12345", city_id="1"):
        """测试影城列表接口"""
        print(f"\n=== 测试影城列表 ===")

        endpoints = [
            f"/ajax/cinemaList?movieId={movie_id}&day={datetime.now().strftime('%Y-%m-%d')}",
            f"/api/cinema/list?movieId={movie_id}",
        ]

        for endpoint in endpoints:
            url = self.base_url + endpoint
            try:
                print(f"尝试: {url}")
                resp = self.session.get(url, timeout=5)
                print(f"状态码: {resp.status_code}")

                if resp.status_code == 200:
                    data = resp.json()
                    print(f"响应: {json.dumps(data, ensure_ascii=False, indent=2)[:500]}")
                    return url, data

            except Exception as e:
                print(f"❌ 请求失败: {e}")

        return None, None

    def test_show_list(self, cinema_id="6789", movie_id="12345"):
        """测试场次列表接口"""
        print(f"\n=== 测试场次列表 ===")

        endpoints = [
            f"/ajax/showList?cinemaId={cinema_id}&movieId={movie_id}",
            f"/api/show/list?cinemaId={cinema_id}&movieId={movie_id}",
        ]

        for endpoint in endpoints:
            url = self.base_url + endpoint
            try:
                print(f"尝试: {url}")
                resp = self.session.get(url, timeout=5)
                print(f"状态码: {resp.status_code}")

                if resp.status_code == 200:
                    data = resp.json()
                    print(f"响应: {json.dumps(data, ensure_ascii=False, indent=2)[:500]}")
                    return url, data

            except Exception as e:
                print(f"❌ 请求失败: {e}")

        return None, None

    def test_seat_map(self, show_id="123456"):
        """测试座位图接口"""
        print(f"\n=== 测试座位图 ===")

        endpoints = [
            f"/ajax/seatMap?showId={show_id}",
            f"/api/seat/map?showId={show_id}",
        ]

        for endpoint in endpoints:
            url = self.base_url + endpoint
            try:
                print(f"尝试: {url}")
                resp = self.session.get(url, timeout=5)
                print(f"状态码: {resp.status_code}")

                if resp.status_code == 200:
                    data = resp.json()
                    print(f"响应: {json.dumps(data, ensure_ascii=False, indent=2)[:500]}")
                    return url, data

            except Exception as e:
                print(f"❌ 请求失败: {e}")

        return None, None

    def analyze_response_structure(self, data):
        """分析响应数据结构"""
        print("\n=== 响应结构分析 ===")

        def analyze_dict(d, prefix=""):
            for key, value in d.items():
                if isinstance(value, dict):
                    print(f"{prefix}{key}: (对象)")
                    analyze_dict(value, prefix + "  ")
                elif isinstance(value, list):
                    print(f"{prefix}{key}: (数组, 长度={len(value)})")
                    if value and isinstance(value[0], dict):
                        print(f"{prefix}  第一项结构:")
                        analyze_dict(value[0], prefix + "    ")
                else:
                    print(f"{prefix}{key}: {type(value).__name__} = {value}")

        if isinstance(data, dict):
            analyze_dict(data)

    def export_cookie_from_browser(self):
        """导出浏览器 Cookie 的帮助说明"""
        print("""
=== 如何导出 Cookie ===

1. 打开 Chrome 浏览器
2. 访问 https://www.maoyan.com 并登录
3. 按 F12 打开开发者工具
4. 切换到 "Application" (应用) 标签
5. 左侧找到 "Cookies" → "https://www.maoyan.com"
6. 复制所有 Cookie 值，格式如下：

   cookie1=value1; cookie2=value2; cookie3=value3

7. 将复制的 Cookie 粘贴到 api_tester.py 的 self.cookie 中

关键 Cookie（可能包含）：
- __mta
- uuid_n_v
- ci
- __permanentid
        """)

    def run_all_tests(self):
        """运行所有测试"""
        print("=" * 60)
        print("猫眼 API 测试工具")
        print("=" * 60)

        if not self.cookie:
            print("\n⚠️  警告: 未设置 Cookie，某些接口可能无法访问")
            self.export_cookie_from_browser()
            print("\n继续测试公开接口...\n")

        # 测试各个接口
        self.test_search("奥德赛")
        self.test_cinema_list()
        self.test_show_list()
        self.test_seat_map()

        print("\n" + "=" * 60)
        print("测试完成")
        print("=" * 60)
        print("""
下一步：
1. 在浏览器中手动操作，用 Network 标签找到真实的 API
2. 记录下完整的请求 URL、Headers、Params
3. 更新 app.py 中的 API 调用逻辑
4. 根据响应结构调整数据解析代码
        """)


def main():
    """主函数"""
    tester = MaoyanAPITester()

    # 设置你的 Cookie（从浏览器复制）
    # tester.cookie = "YOUR_COOKIE_HERE"

    # 运行测试
    tester.run_all_tests()


if __name__ == "__main__":
    main()
