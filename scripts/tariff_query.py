"""
个人探索脚本，不属于产品代码，也不在 Makefile 的 check/test 范围内。

用途：手工验证海关税则查询页（gss.customs.gov.cn）的抓取流程。
依赖 requests（未加入项目依赖），需要时自行安装；脚本在 import 时即发起请求，
只适合用 `python scripts/tariff_query.py` 独立运行。
"""
import requests
from html.parser import HTMLParser

class TokenParser(HTMLParser):
    """
       类功能说明：解析 HTML 获取 token
    """
    def __init__(self):
        """
        初始化函数，继承 HTMLParser
        """
        super().__init__()
        self.token = None

    def handle_starttag(self, tag, attrs):
        """
        原理：当解析到 token 标签时，获取其中的 value 值
        :param tag: 标签名称
        :param attrs: 标签属性
        :return:
        """
        # 只处理 input 标签
        if tag != "input":
            return
        # 将 attrs 转换为字典，方便后续获取属性值
        attrs = dict(attrs)
        if attrs.get("name") == "__RequestVerificationToken":
            self.token = attrs.get("value")

PAGE_URL = "http://gss.customs.gov.cn/clsouter2020/Home/TariffContentSearch"
API_URL = "http://gss.customs.gov.cn/CLSouter2020/TariffContent/GetQueryTaxNum"

# Session会在页面访问和接口请求中保持 cookies
session = requests.Session()
# 站点 WAF 会拦截 python-requests 默认 UA，页面和接口请求都需携带浏览器 UA
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/156.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
})

# 获取页面
page = session.get(PAGE_URL, timeout=30)
# 检查请求是否成功
page.raise_for_status()

parser = TokenParser()
parser.feed(page.text)

if not parser.token:
    raise RuntimeError(
        "页面中没有找到验证字段，需要回到浏览器检查它的实际来源。"
    )

# 请求第一页
payload = {
    "__RequestVerificationToken": parser.token,
    "page": 1,
    "pageSize" : 100,
    "Code_Ts_S": "",
    "G_Name": "",
}

# Cookie 由 Session 自动管理（页面响应的 Set-Cookie 会带入后续请求），不要手工写死旧值
headers = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "http://gss.customs.gov.cn",
    "Referer": (
        "http://gss.customs.gov.cn"
        "/clsouter2020/Home/TariffContentSearch"
    ),
    "X-Requested-With": "XMLHttpRequest",
}

response = session.post(API_URL, data=payload, headers=headers, timeout=30)

print(f"状态码：{response.status_code}")
print(f"响应内容：{response.text}")

response.raise_for_status()



