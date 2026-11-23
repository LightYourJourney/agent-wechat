//! Phase 01 对照实验：把导出热路径里「纯 Python 的逐行解码」搬到 Rust（PyO3）。
//!
//! 只实现 `reader._decode_content` + `_decode_row` 的群前缀剥离这一核心，**语义必须与
//! Python 参考实现逐条一致**（由 bench_native.py 在全部真实行上核对）：
//! - CT=4 → zstd **单帧**解压，帧不完整 / 损坏 → ValueError（Python 侧抛 DecryptError）
//! - 其余 → UTF-8 解码，非法字节按 U+FFFD 替换（对齐 `bytes.decode("utf-8", "replace")`）
//! - 群聊：`<X>:\n正文` 前缀，文本消息剥出发送者；非文本只在 head 以 `@chatroom` 结尾时剥正文
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList, PyString, PyTuple};
use std::cell::RefCell;
use zstd::stream::raw::{Decoder as RawDecoder, InBuffer, Operation, OutBuffer};

const TYPE_TEXT: i64 = 1;

thread_local! {
    // 解压上下文与输出缓冲**跨行复用**：每行新建 DCtx 是第一版比 Python 还慢的主因之一。
    static DEC: RefCell<(RawDecoder<'static>, Vec<u8>)> =
        RefCell::new((RawDecoder::new().expect("zstd dctx"), vec![0u8; 256 * 1024]));
}

/// 只解**第一帧**（对齐 python-zstandard 的 decompressobj）：帧走完即停，后续字节忽略；
/// 输入耗尽而帧未完 → 截断错误。
fn zstd_first_frame(raw: &[u8]) -> PyResult<Vec<u8>> {
    DEC.with(|cell| {
        let mut guard = cell.borrow_mut();
        let (dec, buf) = &mut *guard;
        dec.reinit().map_err(|e| PyValueError::new_err(format!("zstd: {e}")))?;
        let mut out = Vec::with_capacity(raw.len() * 4);
        let mut inp = InBuffer::around(raw);
        loop {
            let mut ob = OutBuffer::around(&mut buf[..]);
            let hint = dec.run(&mut inp, &mut ob)
                .map_err(|e| PyValueError::new_err(format!("zstd: {e}")))?;
            let n = ob.pos();
            out.extend_from_slice(&buf[..n]);
            if hint == 0 {
                return Ok(out);
            }
            if inp.pos() == raw.len() && n < buf.len() {
                return Err(PyValueError::new_err("zstd: 帧不完整（数据被截断）"));
            }
        }
    })
}

/// UTF-8 → str：合法（绝大多数）时零额外拷贝地交给 Python，非法才走 lossy 替换。
fn to_text(bytes: &[u8]) -> std::borrow::Cow<'_, str> {
    match std::str::from_utf8(bytes) {
        Ok(s) => std::borrow::Cow::Borrowed(s),
        Err(_) => String::from_utf8_lossy(bytes),
    }
}

/// 单条：每行一次跨语言调用（量 FFI 的逐次成本）。
#[pyfunction]
#[pyo3(signature = (raw, ct))]
fn decode_one<'py>(py: Python<'py>, raw: Option<&[u8]>, ct: Option<i64>)
    -> PyResult<Option<Bound<'py, PyString>>> {
    let Some(b) = raw else { return Ok(None) };
    let owned;
    let bytes: &[u8] = if ct == Some(4) { owned = zstd_first_frame(b)?; &owned } else { b };
    Ok(Some(PyString::new(py, &to_text(bytes))))
}

/// 批量：rows = [(local_type, message_content: bytes|None, ct: int|None), ...]
/// 返回 [(content: str|None, sender: str|None), ...]
#[pyfunction]
fn decode_rows<'py>(py: Python<'py>, rows: &Bound<'py, PyList>, is_chatroom: bool)
    -> PyResult<Bound<'py, PyList>> {
    let out = PyList::empty(py);
    for item in rows.iter() {
        let t = item.cast::<PyTuple>()?;
        let ltype: i64 = t.get_item(0)?.extract()?;
        let mc = t.get_item(1)?;
        let ct: Option<i64> = t.get_item(2)?.extract()?;
        if mc.is_none() {
            out.append((py.None(), py.None()))?;
            continue;
        }
        let raw = mc.cast::<PyBytes>()?.as_bytes();
        let owned;
        let bytes: &[u8] = if ct == Some(4) { owned = zstd_first_frame(raw)?; &owned } else { raw };
        let text = to_text(bytes);
        let mut content: &str = &text;
        let mut sender: Option<&str> = None;
        let ty = ltype & 0xffff_ffff;
        if is_chatroom && !content.is_empty() {
            if let Some(pos) = content.find(":\n") {
                let (head, rest) = (&content[..pos], &content[pos + 2..]);
                if !head.is_empty() && !head.contains('\n') {
                    if ty == TYPE_TEXT {
                        sender = Some(head);
                        content = rest;
                    } else if head.ends_with("@chatroom") {
                        content = rest;
                    }
                }
            }
        }
        // 切片直接建 Python str，不经中间 String
        out.append((PyString::new(py, content), sender.map(|h| PyString::new(py, h))))?;
    }
    Ok(out)
}

/// FFI 地板：什么都不做，只量「把一批行交给原生代码再拿回来」的固定成本。
#[pyfunction]
fn noop_rows(rows: &Bound<'_, PyList>) -> usize {
    rows.len()
}

#[pyfunction]
fn noop_one(raw: Option<&[u8]>) -> usize {
    raw.map_or(0, |b| b.len())
}

#[pymodule]
fn native_decode(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(decode_one, m)?)?;
    m.add_function(wrap_pyfunction!(decode_rows, m)?)?;
    m.add_function(wrap_pyfunction!(noop_rows, m)?)?;
    m.add_function(wrap_pyfunction!(noop_one, m)?)?;
    Ok(())
}
