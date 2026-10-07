"""集成测试：文件下载路由（``GET /threads/{会话}/files/{路径}``）。

这个端点有两条**安全**承诺，都在这里钉住：

1. **只出 ``/output``**：前缀由路由自己拼，``/workspace`` ``/input`` ``/skills`` 不可达。
2. **可执行内容一律附件**：模型写出来的 ``.html`` / ``.svg`` 若能在浏览器里 inline 打开，
   就等于让它在 API 的源上跑脚本。

还有一条容易漏的**副作用**承诺：查一个不存在的会话**不能**在磁盘上留下目录——构造
``Sandbox`` 会顺手 mkdir，所以路由必须先做一次无副作用的检查。

全部在 ``tmp_path`` 上跑，绝不碰真的 ``backend/.evaai``。
"""

import pytest
import httpx
from fastapi import FastAPI

from app.gateway.routers import files

THREAD = "th-1"


@pytest.fixture(autouse=True)
def _sandbox_root(tmp_path, monkeypatch):
    monkeypatch.setattr("app.tools.sandbox.SANDBOX_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def client() -> httpx.AsyncClient:
    """只挂这一个 router 的应用——CORS、lifespan 这些层与本片无关。

    夹具是**同步**的、直接返回客户端：仓库里没有异步夹具的先例，而 ``ASGITransport`` 不需要
    客户端先 start（它不走网络），所以没有必要为了一个 ``async with`` 引入
    ``pytest_asyncio.fixture``。
    """
    app = FastAPI()
    app.include_router(files.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _seed(root, relative, content="内容", thread=THREAD):
    """在沙箱里造一个 /output 下的文件。"""
    target = root / "user" / thread / "output" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# 正常下发
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_text_file_is_served_inline(_sandbox_root, client):
    _seed(_sandbox_root, "notes.md", "第一行\n第二行")

    response = await client.get(f"/threads/{THREAD}/files/notes.md")

    assert response.status_code == 200
    # 按行比内容，不逐字节比换行风格：写文件走文本模式，Windows 上 `\n` 会落成 `\r\n`。
    # 「下载到的字节与模型写的差一个 CR」是文件工具既有的行为，不是这个端点引入的——
    # 本片如实记在 ADR-0015，不在这里假装它是 LF。
    assert response.text.splitlines() == ["第一行", "第二行"]
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["content-disposition"].startswith("inline")


@pytest.mark.asyncio
async def test_a_range_request_returns_partial_content(_sandbox_root, client):
    """预览只取前 N 字节靠的就是这个。

    ``FileResponse`` 自带 Range 支持——但那是**它的**保证，不是我们写的，而前端把「截断了」
    的判据建在它上面（``status === 206``）。钉在这里：哪天响应类型换了、或上游改了行为，
    会先在这里红，而不是前端悄悄显示半截文件还当成全文。
    """
    _seed(_sandbox_root, "big.txt", "x" * 5000)

    response = await client.get(f"/threads/{THREAD}/files/big.txt", headers={"Range": "bytes=0-1023"})

    assert response.status_code == 206
    assert len(response.text) == 1024
    assert response.headers["content-range"] == "bytes 0-1023/5000"


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_a_range_request_for_a_small_file_returns_206_with_the_whole_file(_sandbox_root, client):
    """**206 不等于「被截断」**——这条是真踩过的坑，写在这里免得下次再踩。

    文件比请求的范围还小时，``FileResponse`` 照样回 206，而且把**全文**给它。所以前端不能
    拿状态码当截断判据（那样每个小文件都会顶着一句「文件较大，只显示了前 256 KB」），要看
    ``Content-Range`` 斜杠**后面**那个总数：它才是文件的真实大小。
    """
    _seed(_sandbox_root, "small.txt", "x" * 100)

    response = await client.get(
        f"/threads/{THREAD}/files/small.txt", headers={"Range": "bytes=0-262143"}
    )

    assert response.status_code == 206, "小文件也回 206——这正是坑所在"
    assert len(response.text) == 100
    assert response.headers["content-range"] == "bytes 0-99/100"


@pytest.mark.asyncio
async def test_a_range_beyond_the_file_end_is_clamped(_sandbox_root, client):
    """``Content-Range`` 的终点被夹到文件末尾，所以「收到的字节数」是可信的。"""
    _seed(_sandbox_root, "small.txt", "x" * 300)

    response = await client.get(
        f"/threads/{THREAD}/files/small.txt", headers={"Range": "bytes=0-99999"}
    )

    assert response.headers["content-range"] == "bytes 0-299/300"
    assert len(response.text) == 300


@pytest.mark.asyncio
async def test_a_nested_path_works(_sandbox_root, client):
    """交付物可以放在子目录里，路径要能带斜杠。"""
    _seed(_sandbox_root, "reports/2026/q1.md", "季报")

    response = await client.get(f"/threads/{THREAD}/files/reports/2026/q1.md")

    assert response.status_code == 200
    assert response.text == "季报"


@pytest.mark.asyncio
async def test_an_unknown_extension_is_served_as_a_stream(_sandbox_root, client):
    """``.go`` 之类认不出来也得能下载——不能因为猜不出类型就 500。"""
    _seed(_sandbox_root, "quicksort.go", "package main")

    response = await client.get(f"/threads/{THREAD}/files/quicksort.go")

    assert response.status_code == 200
    assert response.text == "package main"
    assert response.headers["content-type"].startswith("application/octet-stream")


# --------------------------------------------------------------------------- #
# 可执行内容强制附件
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["evil.html", "evil.htm", "evil.svg", "evil.xhtml", "evil.xml"])
async def test_active_content_is_forced_to_attachment(_sandbox_root, client, name):
    """同源脚本执行面：这些能在浏览器里跑起来，所以只能下载、不能打开。"""
    _seed(_sandbox_root, name, "<script>alert(1)</script>")

    response = await client.get(f"/threads/{THREAD}/files/{name}")

    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")


@pytest.mark.asyncio
async def test_download_true_forces_attachment_for_a_safe_type(_sandbox_root, client):
    """卡片上的按钮写的是「下载」——``.png`` 这类浏览器会自己渲染的类型，
    光靠 inline 点开只会新开一个标签页。"""
    _seed(_sandbox_root, "chart.png", "not-really-a-png")

    response = await client.get(f"/threads/{THREAD}/files/chart.png?download=true")

    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")


@pytest.mark.asyncio
async def test_download_true_is_not_needed_for_active_content(_sandbox_root, client):
    """带不带这个参数，``.html`` 都只能是附件——两道口子不能互相抵消。"""
    _seed(_sandbox_root, "evil.html", "<script>alert(1)</script>")

    response = await client.get(f"/threads/{THREAD}/files/evil.html?download=false")

    assert response.headers["content-disposition"].startswith("attachment")


@pytest.mark.asyncio
async def test_the_active_content_check_is_case_insensitive(_sandbox_root, client):
    """``.HTML`` 与 ``.html`` 是同一个东西，判别不能只看小写写法。"""
    _seed(_sandbox_root, "EVIL.HTML", "<script>alert(1)</script>")

    response = await client.get(f"/threads/{THREAD}/files/EVIL.HTML")

    assert response.headers["content-disposition"].startswith("attachment")


# --------------------------------------------------------------------------- #
# 文件名
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_chinese_filename_is_encoded(_sandbox_root, client):
    """中文名直接写进头会变乱码，所以走 RFC 5987 的 ``filename*``。"""
    _seed(_sandbox_root, "报告.md", "内容")

    response = await client.get(f"/threads/{THREAD}/files/报告.md")

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition
    assert "%E6%8A%A5%E5%91%8A.md" in disposition


# --------------------------------------------------------------------------- #
# 拒绝
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_missing_file_is_404(_sandbox_root, client):
    _seed(_sandbox_root, "a.md")  # 让会话存在

    response = await client.get(f"/threads/{THREAD}/files/never-written.md")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_a_directory_is_404(_sandbox_root, client):
    _seed(_sandbox_root, "sub/keep.md")

    response = await client.get(f"/threads/{THREAD}/files/sub")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_traversal_out_of_output_is_refused(_sandbox_root, client):
    """从 ``/output`` 往外跳要挡死——这是这个端点最要紧的一条。

    ``..`` 用百分号编码送出：先让**路由**收到它，才能验证是路由/沙箱挡住的，
    而不是被某层 URL 归一化顺手吃掉。
    """
    _seed(_sandbox_root, "a.md")
    workspace = _sandbox_root / "user" / THREAD / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "secret.md").write_text("机密", encoding="utf-8")

    response = await client.get(f"/threads/{THREAD}/files/%2e%2e%2fworkspace%2fsecret.md")

    assert response.status_code in (403, 404)
    assert "机密" not in response.text


@pytest.mark.asyncio
async def test_an_unknown_thread_is_404_and_leaves_nothing_on_disk(_sandbox_root, client):
    """**副作用**承诺：一次 404 的请求不该在磁盘上留下目录。

    构造 ``Sandbox`` 会把 ``input``/``output``/``workspace`` 一起建出来，所以路由必须先做
    一次无副作用的检查——这条就是为它写的。
    """
    response = await client.get("/threads/never-existed/files/a.md")

    assert response.status_code == 404
    assert not (_sandbox_root / "user" / "never-existed").exists(), "404 不该建目录"


@pytest.mark.asyncio
async def test_an_illegal_thread_id_is_404(_sandbox_root, client):
    """非法会话 id 会当路径段用，必须在够到文件系统之前挡掉。"""
    response = await client.get("/threads/a%2Fb/files/a.md")

    assert response.status_code == 404
