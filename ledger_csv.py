"""可用 Excel 编辑的 CSV 台账，不包含运行快照和占用登记。"""
import csv
import io
import re


COLUMNS = {
    "IP（必填）": "ip", "名称": "name", "型号": "model", "SSH端口": "ssh_port",
    "SSH用户名": "username", "SSH密码": "password", "组网标签": "tags",
    "UBoE互通组": "uboe", "RoCE互通组": "roce", "UBG互通组": "ubg",
    "预设卡数": "expected_cards", "启用采集": "collect_enabled", "备注": "note",
}


def protect(value):
    text = str(value if value is not None else "")
    # Excel 不能将台账内容解释为公式；导入时可逆还原此转义。
    if text.startswith(("'", "\t", "\r", "\n")) or text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def unprotect(text):
    if len(text) > 1 and text[0] == "'" and (text[1] in "=+-@'\t\r\n" or text[1:].lstrip().startswith(("=", "+", "-", "@"))):
        return text[1:]
    return text


def column_labels(models=(), tags=(), groups=None):
    """选项来自当前台账配置，而不是把型号列表写死在模板里。"""
    labels = list(COLUMNS)
    choices = " / ".join(models) or "请按页面型号选项填写"
    labels[2] = f"型号（可选：{choices}；留空不修改已有值）"
    labels[3] = "SSH端口（1–65535；默认22）"
    labels[4] = "SSH用户名（默认root）"
    labels[6] = f"组网标签（建议：{' / '.join(dict.fromkeys(['UBoE', 'RoCE', 'UBG', *tags]))}；多项用逗号分隔；其他标签可填写）"
    for index, kind in ((7, "uboe"), (8, "roce"), (9, "ubg")):
        existing = " / ".join((groups or {}).get(kind, [])) or "暂无"
        labels[index] += f"（已有组：{existing}；同组请用相同名称，新组可新增，未知留空）"
    labels[10] = "预设卡数（整数0–64；A3通常16，以实际采集为准）"
    labels[11] = "启用采集（可选：是 / 否；新机器默认是）"
    return labels


def export_csv(servers=(), *, models=(), tags=(), groups=None):
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(column_labels(models, tags, groups))
    for server in servers:
        row = dict(server)
        row.update(server.get("network_groups") or {})
        row["tags"] = ",".join(server.get("tags") or [])
        row["collect_enabled"] = "是" if server.get("collect_enabled", True) else "否"
        writer.writerow([protect(row.get(key, "")) for key in COLUMNS.values()])
    return out.getvalue().encode("utf-8-sig")


def normalize_model(value, models, line):
    def key(text):
        return re.sub(r"\s+", "", text.translate(str.maketrans("（）", "()"))).casefold()
    matches = [model for model in models if key(model) == key(value)]
    if len(matches) == 1:
        return matches[0]
    if value in models:
        return value
    raise ValueError(f"第 {line} 行型号不在当前可选项中，请填写：{' / '.join(models)}；新型号请先在页面设置中添加")


def parse_csv(content, *, models=None):
    if len(content) > 2 * 1024 * 1024:
        raise ValueError("CSV 文件不能超过 2 MB")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise ValueError("请用 Excel 另存为 CSV UTF-8 文件") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    headers = reader.fieldnames or []
    aliases = {**COLUMNS, **{key: key for key in COLUMNS.values()}, "IP": "ip"}
    # 括号内为填写提示；兼容旧模板，且配置变化后旧模板依然能识别列。
    mapped = [aliases.get(h.strip()) or aliases.get(h.strip().split("（", 1)[0]) for h in headers]
    if "ip" not in mapped:
        raise ValueError("缺少 IP 列，请先下载导入模板")
    if any(key is None for key in mapped) or len(set(mapped)) != len(mapped):
        raise ValueError("CSV 有未知或重复列，请使用模板中的列名")
    rows = []
    names = set()
    for raw in reader:
        line = reader.line_num
        if None in raw or any(v is None for v in raw.values()):
            raise ValueError(f"第 {line} 行列数不正确")
        if not any(v.strip() for v in raw.values()):
            continue
        item = {}
        for header, key in zip(headers, mapped):
            value = unprotect(raw[header])
            if key != "password":
                value = value.strip()
            if value != "":
                item[key] = value
        ip = item.get("ip", "")
        if not ip or len(ip) > 64 or any(c.isspace() for c in ip):
            raise ValueError(f"第 {line} 行 IP 不能为空、包含空格或超过 64 个字符")
        item["name"] = item.get("name", ip)
        if len(item["name"]) > 60 or item["name"] in names:
            raise ValueError(f"第 {line} 行名称重复或超过 60 个字符")
        names.add(item["name"])
        if "model" in item and models is not None:
            item["model"] = normalize_model(item["model"], models, line)
        for key, lo, hi in (("ssh_port", 1, 65535), ("expected_cards", 0, 64)):
            if key in item:
                try:
                    value = int(item[key])
                except ValueError as exc:
                    raise ValueError(f"第 {line} 行 {key} 必须是整数") from exc
                if not lo <= value <= hi:
                    raise ValueError(f"第 {line} 行 {key} 必须在 {lo}–{hi} 之间")
                item[key] = value
        if "collect_enabled" in item:
            value = item["collect_enabled"].lower()
            if value not in ("是", "否", "1", "0", "true", "false"):
                raise ValueError(f"第 {line} 行启用采集请填 是 或 否")
            item["collect_enabled"] = int(value in ("是", "1", "true"))
        if "tags" in item:
            canonical = {"uboe": "UBoE", "roce": "RoCE", "ubg": "UBG"}
            item["tags"] = list(dict.fromkeys(canonical.get(t.strip().casefold(), t.strip())
                                              for t in item["tags"].replace("，", ",").split(",") if t.strip()))
        groups = {key: item.pop(key) for key in ("uboe", "roce", "ubg") if key in item}
        if any(len(v) > 60 for v in groups.values()):
            raise ValueError(f"第 {line} 行互通组不能超过 60 个字符")
        if groups:
            item["network_groups"] = groups
        rows.append(item)
        if len(rows) > 1000:
            raise ValueError("一次最多导入 1000 台服务器")
    if not rows:
        raise ValueError("没有可导入的数据，请在模板中填写服务器 IP")
    return rows
