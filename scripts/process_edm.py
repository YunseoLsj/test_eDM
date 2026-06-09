#!/usr/bin/env python3
import argparse
import datetime as dt
import email.utils
import hashlib
import json
import math
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import zipfile
import zlib


PNG_SIG = b"\x89PNG\r\n\x1a\n"

FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
NOSTREAM = 0xFFFFFFFF
SECTOR_SIZE = 512
MINI_SECTOR_SIZE = 64
MINI_CUTOFF = 4096


def slugify(value):
    value = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip())
    value = re.sub(r"-{2,}", "-", value).strip("-_")
    return value or "edm"


def parse_source_name(filename):
    stem = pathlib.Path(filename).stem
    if "_" not in stem:
        raise ValueError("filename must follow image_name_link.ext")
    image_name, link_token = stem.rsplit("_", 1)
    return image_name, restore_url(link_token)


def restore_url(token):
    token = token.strip()
    if token.startswith("www."):
        return f"https://{token}"
    if token.startswith("https-"):
        return "https://" + restore_url_rest(token[6:])
    if token.startswith("http-"):
        return "http://" + restore_url_rest(token[5:])
    if token.startswith("https://") or token.startswith("http://"):
        return token
    return token


def restore_url_rest(rest):
    rest = rest.replace("--", "/")
    match = re.match(r"^([^/]+?\.(?:com|net|org|co\.kr|kr|io|ai|dev|cloud|co|jp|edu|gov))(?:-(.+))?$", rest)
    if match and match.group(2):
        return f"{match.group(1)}/{match.group(2)}"
    return rest


def read_png_rgba(path):
    data = pathlib.Path(path).read_bytes()
    if not data.startswith(PNG_SIG):
        raise ValueError(f"{path} is not a PNG file")

    pos = len(PNG_SIG)
    idat = []
    width = height = bit_depth = color_type = interlace = None
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8]
        chunk_data = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _cm, _fm, interlace = struct.unpack(">IIBBBBB", chunk_data)
        elif chunk_type == b"IDAT":
            idat.append(chunk_data)
        elif chunk_type == b"IEND":
            break

    if bit_depth != 8 or interlace != 0:
        raise ValueError("only 8-bit non-interlaced PNGs are supported")
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        raise ValueError(f"unsupported PNG color type: {color_type}")

    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    rows = []
    prev = bytearray(stride)
    index = 0

    for _y in range(height):
        filter_type = raw[index]
        index += 1
        cur = bytearray(raw[index : index + stride])
        index += stride
        for x in range(stride):
            left = cur[x - channels] if x >= channels else 0
            up = prev[x]
            up_left = prev[x - channels] if x >= channels else 0
            if filter_type == 1:
                cur[x] = (cur[x] + left) & 0xFF
            elif filter_type == 2:
                cur[x] = (cur[x] + up) & 0xFF
            elif filter_type == 3:
                cur[x] = (cur[x] + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                cur[x] = (cur[x] + paeth(left, up, up_left)) & 0xFF
            elif filter_type != 0:
                raise ValueError(f"unsupported PNG filter: {filter_type}")

        if color_type == 6:
            rgba = cur
        elif color_type == 2:
            rgba = bytearray()
            for i in range(0, len(cur), 3):
                rgba.extend((cur[i], cur[i + 1], cur[i + 2], 255))
        elif color_type == 0:
            rgba = bytearray()
            for gray in cur:
                rgba.extend((gray, gray, gray, 255))
        else:
            rgba = bytearray()
            for i in range(0, len(cur), 2):
                rgba.extend((cur[i], cur[i], cur[i], cur[i + 1]))
        rows.append(bytes(rgba))
        prev = cur

    return width, height, rows


def paeth(a, b, c):
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def write_png_rgba(path, width, height, rows):
    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    raw = bytearray()
    for row in rows:
        raw.append(0)
        raw.extend(row)
    payload = b"".join(
        [
            PNG_SIG,
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(bytes(raw), 9)),
            chunk(b"IEND", b""),
        ]
    )
    pathlib.Path(path).write_bytes(payload)


def crop_rows(rows, x0, y0, x1, y1):
    return [row[x0 * 4 : x1 * 4] for row in rows[y0:y1]]


def detect_cta(width, height, rows):
    mask = [bytearray(width) for _ in range(height)]
    for y, row in enumerate(rows):
        for x in range(width):
            i = x * 4
            r, g, b, a = row[i], row[i + 1], row[i + 2], row[i + 3]
            if a > 128 and r >= 180 and g <= 90 and b <= 120 and r - g >= 80 and r - b >= 60:
                mask[y][x] = 1

    visited = [bytearray(width) for _ in range(height)]
    candidates = []
    for y in range(height):
        for x in range(width):
            if not mask[y][x] or visited[y][x]:
                continue
            stack = [(x, y)]
            visited[y][x] = 1
            min_x = max_x = x
            min_y = max_y = y
            area = 0
            while stack:
                cx, cy = stack.pop()
                area += 1
                min_x = min(min_x, cx)
                max_x = max(max_x, cx)
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < width and 0 <= ny < height and mask[ny][nx] and not visited[ny][nx]:
                        visited[ny][nx] = 1
                        stack.append((nx, ny))
            box_w = max_x - min_x + 1
            box_h = max_y - min_y + 1
            aspect = box_w / box_h
            if 100 <= box_w <= 360 and 35 <= box_h <= 120 and 2.0 <= aspect <= 7.0 and area >= 2000:
                center_penalty = abs(((min_x + max_x) / 2) - (width / 2)) / width
                top_penalty = 1.0 if min_y < 250 else 0.0
                score = area - center_penalty * 3000 - top_penalty * 5000
                candidates.append(
                    {
                        "bbox": [min_x, min_y, max_x + 1, max_y + 1],
                        "area": area,
                        "score": score,
                        "aspect": aspect,
                    }
                )
    if not candidates:
        raise ValueError("CTA button could not be detected")
    return max(candidates, key=lambda item: item["score"])


def split_vertical_ranges(start, end, max_height=1000):
    ranges = []
    cur = start
    while end - cur > max_height:
        ranges.append((cur, cur + max_height))
        cur += max_height
    if cur < end:
        ranges.append((cur, end))
    return ranges


def table_style(width=None):
    parts = ["border-collapse:collapse", "border-spacing:0", "mso-table-lspace:0pt", "mso-table-rspace:0pt"]
    if width:
        parts.insert(0, f"width:{width}px")
    return "; ".join(parts) + ";"


TD_STYLE = "padding:0; margin:0; font-size:0; line-height:0; mso-line-height-rule:exactly;"
IMG_STYLE = "display:block; border:0; outline:none; text-decoration:none; line-height:0; -ms-interpolation-mode:bicubic;"
A_STYLE = "border:0; text-decoration:none; display:block; line-height:0; font-size:0;"


def image_tag(src, width, height, alt=""):
    return f'<img src="{src}" width="{width}" height="{height}" alt="{alt}" style="{IMG_STYLE}">'


def build_html(subject, width, rows, image_base_url, landing_url):
    body_rows = []
    for row in rows:
        if row["type"] == "image":
            src = f'{image_base_url}/{row["file"]}'
            body_rows.append(
                "    <tr>\n"
                f'      <td style="{TD_STYLE}">{image_tag(src, width, row["height"])}</td>\n'
                "    </tr>"
            )
        else:
            left_src = f'{image_base_url}/{row["left"]["file"]}'
            button_src = f'{image_base_url}/{row["button"]["file"]}'
            right_src = f'{image_base_url}/{row["right"]["file"]}'
            body_rows.append(
                "    <tr>\n"
                f'      <td style="{TD_STYLE}">\n'
                f'        <table border="0" cellpadding="0" cellspacing="0" role="presentation" width="{width}" style="{table_style(width)}">\n'
                "          <tr>\n"
                f'            <td width="{row["left"]["width"]}" style="{TD_STYLE}">{image_tag(left_src, row["left"]["width"], row["height"])}</td>\n'
                f'            <td width="{row["button"]["width"]}" style="{TD_STYLE}"><a href="{landing_url}" target="_blank" style="{A_STYLE}">{image_tag(button_src, row["button"]["width"], row["height"], "CTA")}</a></td>\n'
                f'            <td width="{row["right"]["width"]}" style="{TD_STYLE}">{image_tag(right_src, row["right"]["width"], row["height"])}</td>\n'
                "          </tr>\n"
                "        </table>\n"
                "      </td>\n"
                "    </tr>"
            )
    return (
        "<!doctype html>\n"
        '<html lang="ko">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"  <title>{subject}</title>\n"
        "</head>\n"
        '<body style="margin:0; padding:0; background-color:#ffffff;">\n'
        f'  <table border="0" cellpadding="0" cellspacing="0" role="presentation" width="{width}" align="center" style="{table_style(width)}">\n'
        + "\n".join(body_rows)
        + "\n"
        "  </table>\n"
        "</body>\n"
        "</html>\n"
    )


def html_to_eml(html, output_path, subject):
    now = email.utils.format_datetime(dt.datetime.now(dt.timezone.utc))
    content = (
        "From: eDM Automation <no-reply@example.com>\r\n"
        "To: Recipient <recipient@example.com>\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {now}\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: text/html; charset="UTF-8"\r\n'
        "Content-Transfer-Encoding: 8bit\r\n"
        "\r\n"
        f"{html}\r\n"
    )
    with pathlib.Path(output_path).open("w", encoding="utf-8", newline="\r\n") as handle:
        handle.write(content)


def _chunks(data, size):
    return [data[i : i + size] for i in range(0, len(data), size)] or [b""]


def _pad(data, size):
    return data + (b"\x00" * ((size - len(data) % size) % size))


def _cfb_name(name):
    encoded = (name + "\x00").encode("utf-16le")
    if len(encoded) > 64:
        raise ValueError(f"CFB directory name too long: {name}")
    return encoded + (b"\x00" * (64 - len(encoded))), len(encoded)


def _clsid(value):
    if not value:
        return b"\x00" * 16
    match = re.fullmatch(
        r"([0-9A-Fa-f]{8})-([0-9A-Fa-f]{4})-([0-9A-Fa-f]{4})-([0-9A-Fa-f]{4})-([0-9A-Fa-f]{12})",
        value,
    )
    if not match:
        raise ValueError(f"Invalid CLSID: {value}")
    a, b, c, d, e = match.groups()
    return struct.pack("<IHH", int(a, 16), int(b, 16), int(c, 16)) + bytes.fromhex(d + e)


def _dir_entry(name, obj_type, left, right, child, start_sector, size, clsid=""):
    name_buf, name_len = _cfb_name(name)
    return struct.pack(
        "<64sHBBIII16sIQQIQ",
        name_buf,
        name_len,
        obj_type,
        1,
        left,
        right,
        child,
        _clsid(clsid),
        0,
        0,
        0,
        start_sector,
        size,
    )


def _stream_entry(prop_tag, flags, value):
    return struct.pack("<IIQ", prop_tag, flags, value)


def _unicode_stream(text):
    return (text + "\x00").encode("utf-16le")


def _properties_stream(subject, html_bytes, body_text):
    message_class = _unicode_stream("IPM.Note")
    subject_bytes = _unicode_stream(subject)
    body_bytes = _unicode_stream(body_text)
    header = b"\x00" * 8
    header += struct.pack("<IIII", 0, 0, 0, 0)
    header += b"\x00" * 8
    entries = [
        _stream_entry(0x001A001F, 0x00000002, len(message_class)),
        _stream_entry(0x0037001F, 0x00000002, len(subject_bytes)),
        _stream_entry(0x0E1D001F, 0x00000002, len(subject_bytes)),
        _stream_entry(0x1000001F, 0x00000002, len(body_bytes)),
        _stream_entry(0x10130102, 0x00000002, len(html_bytes)),
        _stream_entry(0x0E070003, 0x00000002, 0),
        _stream_entry(0x0E060040, 0x00000002, 0),
    ]
    return header + b"".join(entries)


def _build_cfb(streams):
    streams = sorted(streams, key=lambda item: item[0].upper())
    regular_sectors = []
    chains = []
    directory_records = []
    mini_stream = bytearray()
    mini_fat = []

    for name, data in streams:
        if len(data) < MINI_CUTOFF:
            start_mini = len(mini_fat)
            pieces = _chunks(data, MINI_SECTOR_SIZE)
            for index, piece in enumerate(pieces):
                mini_stream.extend(_pad(piece, MINI_SECTOR_SIZE))
                mini_fat.append(start_mini + index + 1 if index < len(pieces) - 1 else ENDOFCHAIN)
            directory_records.append((name, start_mini, len(data), True))
        else:
            start = len(regular_sectors)
            pieces = _chunks(data, SECTOR_SIZE)
            for piece in pieces:
                regular_sectors.append(_pad(piece, SECTOR_SIZE))
            chains.append((start, len(pieces)))
            directory_records.append((name, start, len(data), False))

    mini_stream_size = len(mini_stream)
    root_start = ENDOFCHAIN
    if mini_stream:
        root_start = len(regular_sectors)
        mini_pieces = _chunks(_pad(bytes(mini_stream), SECTOR_SIZE), SECTOR_SIZE)
        regular_sectors.extend(mini_pieces)
        chains.append((root_start, len(mini_pieces)))

    entries = [
        _dir_entry(
            "Root Entry",
            5,
            NOSTREAM,
            NOSTREAM,
            1 if directory_records else NOSTREAM,
            root_start,
            mini_stream_size,
            "00020D0B-0000-0000-C000-000000000046",
        )
    ]
    for index, (name, start, size, _is_mini) in enumerate(directory_records, start=1):
        right = index + 1 if index < len(directory_records) else NOSTREAM
        entries.append(_dir_entry(name, 2, NOSTREAM, right, NOSTREAM, start, size))
    directory_stream = _pad(b"".join(entries), SECTOR_SIZE)

    dir_start = len(regular_sectors)
    dir_pieces = _chunks(directory_stream, SECTOR_SIZE)
    regular_sectors.extend(dir_pieces)
    chains.append((dir_start, len(dir_pieces)))

    minifat_start = ENDOFCHAIN
    minifat_count = 0
    if mini_fat:
        minifat_start = len(regular_sectors)
        mini_fat_bytes = _pad(b"".join(struct.pack("<I", value) for value in mini_fat), SECTOR_SIZE)
        minifat_pieces = _chunks(mini_fat_bytes, SECTOR_SIZE)
        minifat_count = len(minifat_pieces)
        regular_sectors.extend(minifat_pieces)
        chains.append((minifat_start, minifat_count))

    nonfat_count = len(regular_sectors)
    fat_count = 1
    while math.ceil((nonfat_count + fat_count) / 128) != fat_count:
        fat_count = math.ceil((nonfat_count + fat_count) / 128)
    total_sectors = nonfat_count + fat_count

    fat = [FREESECT] * total_sectors
    for start, count in chains:
        for offset in range(count):
            fat[start + offset] = start + offset + 1 if offset < count - 1 else ENDOFCHAIN
    for sector in range(nonfat_count, total_sectors):
        fat[sector] = FATSECT

    fat_bytes = _pad(b"".join(struct.pack("<I", value) for value in fat), fat_count * SECTOR_SIZE)
    fat_sectors = _chunks(fat_bytes, SECTOR_SIZE)

    difat = [nonfat_count + i for i in range(fat_count)]
    difat.extend([FREESECT] * (109 - len(difat)))
    header = bytearray(SECTOR_SIZE)
    header[0:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    header[24:26] = struct.pack("<H", 0x003E)
    header[26:28] = struct.pack("<H", 0x0003)
    header[28:30] = struct.pack("<H", 0xFFFE)
    header[30:32] = struct.pack("<H", 9)
    header[32:34] = struct.pack("<H", 6)
    header[44:48] = struct.pack("<I", fat_count)
    header[48:52] = struct.pack("<I", dir_start)
    header[56:60] = struct.pack("<I", MINI_CUTOFF)
    header[60:64] = struct.pack("<I", minifat_start)
    header[64:68] = struct.pack("<I", minifat_count)
    header[68:72] = struct.pack("<I", ENDOFCHAIN)
    header[76:512] = b"".join(struct.pack("<I", value) for value in difat)
    return bytes(header) + b"".join(regular_sectors) + b"".join(fat_sectors)


def html_to_oft(html, output_path, subject):
    html_bytes = html.encode("utf-8")
    body_text = subject
    subject_bytes = _unicode_stream(subject)
    streams = [
        ("__properties_version1.0", _properties_stream(subject, html_bytes, body_text)),
        ("__substg1.0_001A001F", _unicode_stream("IPM.Note")),
        ("__substg1.0_0037001F", subject_bytes),
        ("__substg1.0_0E1D001F", subject_bytes),
        ("__substg1.0_1000001F", _unicode_stream(body_text)),
        ("__substg1.0_10130102", html_bytes),
    ]
    pathlib.Path(output_path).write_bytes(_build_cfb(streams))


def validate_html(path):
    text = pathlib.Path(path).read_text(encoding="utf-8")
    lowered = text.lower()
    forbidden = ["<script", "<map", "<area", "position:absolute", "background-image", "<div"]
    hits = [item for item in forbidden if item in lowered]
    if hits:
        raise ValueError(f"forbidden HTML constructs found: {hits}")
    if re.search(r'href="[^"]*[\r\n][^"]*"', text):
        raise ValueError("href contains a newline")
    for img in re.findall(r"<img\b[^>]*>", text, flags=re.IGNORECASE):
        if not re.search(r'\bwidth="\d+"', img) or not re.search(r'\bheight="\d+"', img):
            raise ValueError(f"image without explicit dimensions found: {img}")


def validate_reconstruction(width, height, source_rows, layout_rows, image_dir):
    reconstructed = []
    for row in layout_rows:
        if row["type"] == "image":
            w, h, rows = read_png_rgba(image_dir / row["file"])
            if w != width or h != row["height"]:
                raise ValueError(f"unexpected slice size: {row['file']}")
            reconstructed.extend(rows)
        else:
            left = read_png_rgba(image_dir / row["left"]["file"])
            button = read_png_rgba(image_dir / row["button"]["file"])
            right = read_png_rgba(image_dir / row["right"]["file"])
            if left[1] != row["height"] or button[1] != row["height"] or right[1] != row["height"]:
                raise ValueError("CTA row slice heights do not match")
            for y in range(row["height"]):
                reconstructed.append(left[2][y] + button[2][y] + right[2][y])
    if len(reconstructed) != height:
        raise ValueError("reconstructed height mismatch")
    for y, (src, rec) in enumerate(zip(source_rows, reconstructed)):
        if src != rec:
            raise ValueError(f"reconstructed pixels differ at row {y}")


def resolve_raw_base(remote_url, branch, slug):
    match = re.search(r"github\.com[:/]([^/]+)/([^/.]+)(?:\.git)?$", remote_url)
    if not match:
        raise ValueError(f"cannot infer GitHub raw URL from remote: {remote_url}")
    owner, repo = match.groups()
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{slug}/images"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--original-name", required=True)
    parser.add_argument("--output-root", default=".")
    parser.add_argument("--remote-url", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--drive-file-id", default="")
    args = parser.parse_args()

    output_root = pathlib.Path(args.output_root)
    image_name, landing_url = parse_source_name(args.original_name)
    slug = slugify(image_name)
    work_dir = output_root / slug
    image_dir = work_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    source_path = pathlib.Path(args.source)
    ext = source_path.suffix.lower() or pathlib.Path(args.original_name).suffix.lower() or ".png"
    target_source = work_dir / f"source{ext}"
    if source_path.resolve() != target_source.resolve():
        shutil.copyfile(source_path, target_source)

    width, height, source_rows = read_png_rgba(target_source)
    cta = detect_cta(width, height, source_rows)
    x0, y0, x1, y1 = cta["bbox"]
    button_x0 = max(0, x0 - 3)
    button_x1 = min(width, x1 + 4)
    row_y0 = max(0, y0 - 7)
    row_y1 = min(height, y1 + 28)

    if width < 600 or width > 750:
        raise ValueError(f"eDM width {width}px is outside the recommended 600-750px range")
    if row_y1 <= row_y0 or button_x1 <= button_x0:
        raise ValueError("invalid CTA slice bounds")

    layout_rows = []
    full_index = 1
    for start, end in split_vertical_ranges(0, row_y0):
        name = f"img_{full_index:02d}.png"
        write_png_rgba(image_dir / name, width, end - start, crop_rows(source_rows, 0, start, width, end))
        layout_rows.append({"type": "image", "file": name, "height": end - start, "y0": start, "y1": end})
        full_index += 1

    cta_h = row_y1 - row_y0
    cta_row = {
        "type": "cta",
        "height": cta_h,
        "y0": row_y0,
        "y1": row_y1,
        "left": {"file": "cta_left.png", "width": button_x0},
        "button": {"file": "cta_button.png", "width": button_x1 - button_x0},
        "right": {"file": "cta_right.png", "width": width - button_x1},
    }
    write_png_rgba(image_dir / "cta_left.png", button_x0, cta_h, crop_rows(source_rows, 0, row_y0, button_x0, row_y1))
    write_png_rgba(
        image_dir / "cta_button.png",
        button_x1 - button_x0,
        cta_h,
        crop_rows(source_rows, button_x0, row_y0, button_x1, row_y1),
    )
    write_png_rgba(image_dir / "cta_right.png", width - button_x1, cta_h, crop_rows(source_rows, button_x1, row_y0, width, row_y1))
    layout_rows.append(cta_row)

    for start, end in split_vertical_ranges(row_y1, height):
        name = f"img_{full_index:02d}.png"
        write_png_rgba(image_dir / name, width, end - start, crop_rows(source_rows, 0, start, width, end))
        layout_rows.append({"type": "image", "file": name, "height": end - start, "y0": start, "y1": end})
        full_index += 1

    raw_base = resolve_raw_base(args.remote_url, args.branch, slug)
    server_html = build_html(slug, width, layout_rows, raw_base, landing_url)
    local_html = build_html(slug, width, layout_rows, "images", landing_url)
    server_html_path = work_dir / f"{slug}.html"
    local_html_path = work_dir / f"{slug}_local.html"
    server_html_path.write_text(server_html, encoding="utf-8")
    local_html_path.write_text(local_html, encoding="utf-8")
    html_to_eml(server_html, work_dir / f"{slug}.eml", slug)

    oft_status = "created"
    oft_error = ""
    try:
        html_to_oft(server_html, work_dir / f"{slug}.oft", slug)
    except Exception as exc:
        oft_status = "failed"
        oft_error = str(exc)

    with zipfile.ZipFile(work_dir / "images.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for image_path in sorted(image_dir.glob("*.png")):
            archive.write(image_path, pathlib.Path("images") / image_path.name)

    validate_reconstruction(width, height, source_rows, layout_rows, image_dir)
    validate_html(server_html_path)
    validate_html(local_html_path)

    source_hash = hashlib.sha256(target_source.read_bytes()).hexdigest()
    summary = {
        "slug": slug,
        "original_name": args.original_name,
        "drive_file_id": args.drive_file_id,
        "source": str(target_source),
        "source_sha256": source_hash,
        "width": width,
        "height": height,
        "landing_url": landing_url,
        "cta_detected_bbox": cta["bbox"],
        "cta_slice": {"x0": button_x0, "x1": button_x1, "y0": row_y0, "y1": row_y1},
        "server_image_base_url": raw_base,
        "html": str(server_html_path),
        "local_html": str(local_html_path),
        "eml": str(work_dir / f"{slug}.eml"),
        "oft": str(work_dir / f"{slug}.oft") if oft_status == "created" else None,
        "oft_status": oft_status,
        "oft_error": oft_error,
        "images_zip": str(work_dir / "images.zip"),
        "layout_rows": layout_rows,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "drive_upload_status": "pending_upload",
    }
    (work_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (work_dir / "processing.log").write_text(
        "\n".join(
            [
                f"generated_at={summary['generated_at']}",
                f"original_name={args.original_name}",
                f"slug={slug}",
                f"landing_url={landing_url}",
                f"source_sha256={source_hash}",
                f"size={width}x{height}",
                f"cta_detected_bbox={cta['bbox']}",
                f"cta_slice={summary['cta_slice']}",
                "html_validation=passed",
                "pixel_reconstruction=passed",
                f"oft_status={oft_status}",
                f"oft_error={oft_error}",
                "outlook_render_check=not_available_in_current_environment",
                "drive_upload_status=pending_upload",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    processed_path = output_root / "processed.json"
    if processed_path.exists():
        processed = json.loads(processed_path.read_text(encoding="utf-8"))
    else:
        processed = {}
    processed_key = args.drive_file_id or args.original_name
    processed[processed_key] = {
        "status": "local_complete",
        "slug": slug,
        "original_name": args.original_name,
        "drive_file_id": args.drive_file_id,
        "source_sha256": source_hash,
        "landing_url": landing_url,
        "server_image_base_url": raw_base,
        "generated_at": summary["generated_at"],
    }
    processed_path.write_text(json.dumps(processed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
