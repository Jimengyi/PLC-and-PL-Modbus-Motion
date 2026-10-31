"""CSV 讀寫：編碼判別（UTF-8 有無 BOM、Big5）與保留原文位置的解析。

解析結果帶每個欄位在原文中的位置，寫回單一欄位時只替換那一段文字，其餘字元原樣保留。
"""

from dataclasses import dataclass

BOM = b"\xef\xbb\xbf"


class CsvFormatError(ValueError):
    def __init__(self, line: int, reason: str):
        super().__init__(f"第 {line} 列：{reason}")
        self.line = line
        self.reason = reason


@dataclass(frozen=True)
class CsvRecord:
    line: int  # 該筆資料在檔案中的起始列號（1 起算）
    values: list[str]
    spans: list[tuple[int, int]]  # 每個欄位在原文中的 [start, end)，含引號


def decode_csv_bytes(data: bytes) -> str:
    """UTF-8 含 BOM、UTF-8 無 BOM、Big5（以 cp950 解碼）依序嘗試；都不行 raise ValueError。"""
    if data.startswith(BOM):
        return data[len(BOM) :].decode("utf-8")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp950")
    except UnicodeDecodeError as e:
        raise ValueError("編碼無法辨識（只接受 UTF-8 或 Big5）") from e


def encode_csv_text(text: str) -> bytes:
    """程式寫出的 CSV 一律為 UTF-8 含 BOM。"""
    return BOM + text.encode("utf-8")


def _is_newline_char(c: str) -> bool:
    return c == "\r" or c == "\n"


def parse_csv(text: str) -> list[CsvRecord]:
    """解析逗號分隔、雙引號跳脫（""）的 CSV；跳過空白列。"""
    records: list[CsvRecord] = []
    n = len(text)
    i = 0
    line = 1
    while i < n:
        start_line = line
        values: list[str] = []
        spans: list[tuple[int, int]] = []
        while True:
            start = i
            if i < n and text[i] == '"':
                i += 1
                buf: list[str] = []
                while True:
                    if i >= n:
                        raise CsvFormatError(start_line, "引號未結束")
                    c = text[i]
                    if c == '"':
                        if i + 1 < n and text[i + 1] == '"':
                            buf.append('"')
                            i += 2
                            continue
                        i += 1
                        break
                    if c == "\n" or (c == "\r" and not (i + 1 < n and text[i + 1] == "\n")):
                        line += 1
                    buf.append(c)
                    i += 1
                if i < n and text[i] != "," and not _is_newline_char(text[i]):
                    raise CsvFormatError(line, "引號結束後有多餘字元")
                value = "".join(buf)
            else:
                while i < n and text[i] != "," and not _is_newline_char(text[i]):
                    i += 1
                value = text[start:i]
            values.append(value)
            spans.append((start, i))
            if i < n and text[i] == ",":
                i += 1
                continue
            break
        if i < n:
            i += 2 if text.startswith("\r\n", i) else 1
            line += 1
        if any(v.strip() for v in values):
            records.append(CsvRecord(start_line, values, spans))
    return records


def replace_spans(text: str, replacements: list[tuple[tuple[int, int], str]]) -> str:
    """把多段 [start, end) 換成新文字（各段不可重疊）。"""
    out = text
    for (start, end), new in sorted(replacements, key=lambda r: r[0][0], reverse=True):
        out = out[:start] + new + out[end:]
    return out
