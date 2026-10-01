"""Compare explicit compositor shortcuts, including configured physical keys."""
import ast
import ctypes
import ctypes.util
from pathlib import Path
import re

MODIFIERS = {"SUPER": 64, "META": 64, "MOD4": 64, "ALT": 8, "MOD1": 8,
             "CTRL": 4, "CONTROL": 4, "SHIFT": 1}
ALIASES = {"ENTER": "RETURN", "ESC": "ESCAPE", "PAGE_UP": "PAGEUP", "PRIOR": "PAGEUP",
           "PGUP": "PAGEUP", "PAGE_DOWN": "PAGEDOWN", "NEXT": "PAGEDOWN", "PGDN": "PAGEDOWN",
           "SPACEBAR": "SPACE", " ": "SPACE", ",": "COMMA", ".": "PERIOD", "/": "SLASH",
           "-": "MINUS", "=": "EQUAL", "[": "BRACKETLEFT", "]": "BRACKETRIGHT",
           "LEFTBRACKET": "BRACKETLEFT", "RIGHTBRACKET": "BRACKETRIGHT"}


def key_name(value):
    if value is None:
        return ""
    value = str(value).upper()
    return ALIASES.get(value, ALIASES.get(value.replace(" ", "_").replace("-", "_"), value))


def chord(value):
    parts = [p.strip().upper() for p in value.split("+")]
    if not parts or not parts[-1]:
        raise ValueError("Shortcut has no key")
    try:
        return sum({MODIFIERS[p] for p in parts[:-1]}), key_name(parts[-1])
    except KeyError as exc:
        raise ValueError("Unknown shortcut modifier: " + exc.args[0]) from exc


def physical_names(codes, layout="us", variant=""):
    """Translate XKB positions, conservatively covering every group and level."""
    if not codes:
        return {}
    library = ctypes.util.find_library("xkbcommon")
    if not library:
        return {}
    try:
        lib = ctypes.CDLL(library)
    except OSError:
        return {}
    class Names(ctypes.Structure):
        _fields_ = [(p, ctypes.c_char_p) for p in ("rules", "model", "layout", "variant", "options")]
    lib.xkb_context_new.argtypes = [ctypes.c_int]
    lib.xkb_context_new.restype = ctypes.c_void_p
    lib.xkb_keymap_new_from_names.argtypes = [ctypes.c_void_p, ctypes.POINTER(Names), ctypes.c_int]
    lib.xkb_keymap_new_from_names.restype = ctypes.c_void_p
    lib.xkb_keymap_num_layouts_for_key.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.xkb_keymap_num_layouts_for_key.restype = ctypes.c_uint32
    lib.xkb_keymap_num_levels_for_key.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
    lib.xkb_keymap_num_levels_for_key.restype = ctypes.c_uint32
    lib.xkb_keymap_key_get_syms_by_level.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                                    ctypes.c_uint32, ctypes.POINTER(ctypes.POINTER(ctypes.c_uint32))]
    lib.xkb_keymap_key_get_syms_by_level.restype = ctypes.c_int
    lib.xkb_keysym_get_name.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t]
    lib.xkb_keymap_unref.argtypes = [ctypes.c_void_p]
    lib.xkb_context_unref.argtypes = [ctypes.c_void_p]
    context = lib.xkb_context_new(0)
    if not context:
        return {}
    keymap = None
    try:
        names = Names(None, None, layout.encode(), variant.encode() or None, None)
        keymap = lib.xkb_keymap_new_from_names(context, ctypes.byref(names), 0)
        if not keymap:
            return {}
        result = {}
        for code in codes:
            result[code] = set()
            if not isinstance(code, int) or not 0 < code < 65536:
                continue
            for group in range(lib.xkb_keymap_num_layouts_for_key(keymap, code)):
                for level in range(lib.xkb_keymap_num_levels_for_key(keymap, code, group)):
                    symbols = ctypes.POINTER(ctypes.c_uint32)()
                    count = lib.xkb_keymap_key_get_syms_by_level(keymap, code, group, level, ctypes.byref(symbols))
                    for i in range(count):
                        name = ctypes.create_string_buffer(128)
                        if lib.xkb_keysym_get_name(symbols[i], name, len(name)) > 0:
                            result[code].add(key_name(name.value.decode()))
        return result
    finally:
        if keymap:
            lib.xkb_keymap_unref(keymap)
        lib.xkb_context_unref(context)


def _tokens(source):
    """Lex the small Lua subset without interpreting comments or arbitrary code."""
    pattern = re.compile(r'--\[(=*)\[.*?\]\1\]|--[^\n]*|"(?:[^"\\]|\\.)*"|'
                         r"'(?:[^'\\]|\\.)*'|\[(=*)\[.*?\]\2\]|\.\.|[A-Za-z_]\w*|\d+|[^\s]", re.S)
    return [m[0] for m in pattern.finditer(source) if not m[0].startswith("--")]


def _expression(tokens, variables, start=0):
    """Read only literals, known locals, tostring, concatenation, and +/- integers."""
    def atom(index):
        if index >= len(tokens):
            raise ValueError("Incomplete Lua expression")
        token = tokens[index]
        if token.startswith(('"', "'")):
            return ast.literal_eval(token), index + 1
        if token.isdigit():
            return int(token), index + 1
        if token == "nil":
            return "", index + 1
        if token in ("(", "tostring"):
            index += token == "tostring"
            if index >= len(tokens) or tokens[index] != "(":
                raise ValueError("Unsupported Lua call")
            value, end = read(index + 1)
            if end >= len(tokens) or tokens[end] != ")":
                raise ValueError("Unclosed Lua expression")
            return (str(value) if token == "tostring" else value), end + 1
        if token in variables:
            return variables[token], index + 1
        raise ValueError("Unsupported Lua expression")

    def arithmetic(index):
        value, index = atom(index)
        while index < len(tokens) and tokens[index] in ("+", "-"):
            operation = tokens[index]
            other, index = atom(index + 1)
            if type(value) is not int or type(other) is not int:
                raise ValueError("Unsupported Lua arithmetic")
            value += other * (1 if operation == "+" else -1)
        return value, index

    def read(index):
        value, index = arithmetic(index)
        while index < len(tokens) and tokens[index] == "..":
            other, index = arithmetic(index + 1)
            value = str(value) + str(other)
        return value, index

    return read(start)


def _arguments(tokens, opening):
    depth, start, arguments = 0, opening + 1, []
    for index in range(opening, len(tokens)):
        token = tokens[index]
        if token in ("(", "{", "["):
            depth += 1
        elif token in (")", "}", "]"):
            depth -= 1
            if depth == 0:
                return arguments + [tokens[start:index]], index + 1
        elif token == "," and depth == 1:
            arguments.append(tokens[start:index])
            start = index + 1
    raise ValueError("Unclosed Lua binding")


def _block_end(tokens, start):
    depth, pending_do = 1, 0
    for index in range(start, len(tokens)):
        token = tokens[index]
        if token in ("for", "while"):
            depth += 1
            pending_do += 1
        elif token in ("if", "function", "repeat"):
            depth += 1
        elif token == "do":
            if pending_do:
                pending_do -= 1
            else:
                depth += 1
        elif token in ("end", "until"):
            depth -= 1
            if not depth:
                return index
    return len(tokens)


def source_positions(paths):
    """Recover physical binds omitted by some Hyprland JSON versions.

    Inspect literal declarations, literal local aliases, and bounded numeric
    loops. Unknown entries are not guessed; collisions() then fails closed.
    Source text is never executed.
    """
    positions = {}

    def scan(tokens, variables, budget, depth=0):
        if depth > 16:
            return
        index = 0
        while index < len(tokens) and budget[0] > 0:
            budget[0] -= 1
            token = tokens[index]
            if token == "for" and index + 3 < len(tokens) and tokens[index + 2] == "=":
                try:
                    lower, end = _expression(tokens, variables, index + 3)
                    if tokens[end] != ",":
                        raise ValueError()
                    upper, end = _expression(tokens, variables, end + 1)
                    step = 1
                    if tokens[end] == ",":
                        step, end = _expression(tokens, variables, end + 1)
                    if tokens[end] != "do":
                        raise ValueError()
                    closing = _block_end(tokens, end + 1)
                    if all(type(n) is int for n in (lower, upper, step)) and step > 0 and 0 <= lower <= upper <= 4096:
                        values = range(lower, upper + 1, step)
                        if len(values) <= 256:
                            for n in values:
                                scan(tokens[end + 1:closing], {**variables, tokens[index + 1]: n}, budget, depth + 1)
                    index = closing + 1
                    continue
                except (ValueError, SyntaxError, IndexError):
                    pass
            local_assignment = token == "local" and index + 2 < len(tokens) and tokens[index + 2] == "="
            known_assignment = token in variables and index + 1 < len(tokens) and tokens[index + 1] == "="
            if local_assignment or known_assignment:
                name = tokens[index + 1] if local_assignment else token
                try:
                    value, end = _expression(tokens, variables, index + (3 if local_assignment else 2))
                    if end < len(tokens) and tokens[end] in (".", ":", "(", "[", "*", "/", "%", "^", "=", "~"):
                        raise ValueError("Unsupported assignment suffix")
                    variables[name] = value
                    index = end
                    continue
                except (ValueError, SyntaxError):
                    variables.pop(name, None)
            if (index + 3 < len(tokens) and token in ("o", "hl") and tokens[index + 1] == "."
                    and tokens[index + 2] in ("bind", "bind_toggle") and tokens[index + 3] == "("):
                try:
                    arguments, end = _arguments(tokens, index + 3)
                    keys, consumed = _expression(arguments[0], variables)
                    if consumed != len(arguments[0]) or not isinstance(keys, str):
                        raise ValueError()
                    if token == "o":
                        description, consumed = _expression(arguments[1], variables)
                        if consumed != len(arguments[1]):
                            raise ValueError()
                    else:
                        description = ""
                        options = arguments[2] if len(arguments) > 2 else []
                        for field in range(len(options) - 2):
                            if options[field:field + 2] == ["description", "="]:
                                description, _ = _expression(options, variables, field + 2)
                                break
                    mask, key = chord(keys)
                    if re.fullmatch(r"CODE:\d+", key):
                        positions.setdefault((mask, str(description)), set()).add(int(key[5:]))
                    index = end
                    continue
                except (ValueError, SyntaxError, IndexError):
                    pass
            index += 1

    for path in paths:
        try:
            source = Path(path).read_text()
        except (OSError, UnicodeError):
            continue
        scan(_tokens(source), {}, [100000])
    return positions


def collisions(mappings, registered, *, positions=None, layout="us", variant=""):
    mappings, registered = list(mappings), list(registered)
    positions = positions or {}
    codes = {b.get("keycode", 0) for b in registered if b.get("keycode")}
    codes.update(c for values in positions.values() for c in values)
    for keys, _, _ in mappings:
        name = chord(keys)[1]
        if re.fullmatch(r"CODE:\d+", name):
            codes.add(int(name[5:]))
    for bind in registered:
        name = key_name(bind.get("key", ""))
        if re.fullmatch(r"CODE:\d+", name):
            codes.add(int(name[5:]))
    aliases = physical_names(codes, layout, variant)
    conflicts = {}
    for keys, action, _ in mappings:
        mask, name = chord(keys)
        proposed = int(name[5:]) if re.fullmatch(r"CODE:\d+", name) else None
        proposed_names = aliases.get(proposed, set()) if proposed else {name}
        reasons = []
        if proposed and not proposed_names:
            reasons.append("Cannot resolve proposed physical shortcut: " + keys)
        for other_keys, other_action, _ in mappings:
            if other_action == action:
                continue
            other_mask, other_name = chord(other_keys)
            other_code = int(other_name[5:]) if re.fullmatch(r"CODE:\d+", other_name) else None
            other_names = aliases.get(other_code, set()) if other_code else {other_name}
            if mask == other_mask and (proposed_names & other_names or proposed and proposed == other_code):
                reasons.append("Profile also assigns this shortcut to " + other_action)
        for bind in registered:
            universal = str(bind.get("submap_universal", False)).lower() == "true"
            if bind.get("modmask") != mask or (bind.get("submap") not in (None, "", "reset") and not universal):
                continue
            if bind.get("catch_all"):
                reasons.append(bind.get("description") or "Catch-all shortcut")
                continue
            candidate = key_name(bind.get("key", ""))
            physical = {bind["keycode"]} if bind.get("keycode") else (
                positions.get((mask, bind.get("description", "")), set()) if not candidate else set())
            if re.fullmatch(r"CODE:\d+", candidate):
                physical = {int(candidate[5:])}
                candidate = ""
            # Physical identity takes precedence if both fields are populated.
            if physical:
                hit = proposed in physical if proposed else any(name in aliases.get(c, set()) for c in physical)
                if any(not aliases.get(c) for c in physical):
                    reasons.append("Cannot resolve a physical shortcut: " + bind.get("description", "Unknown"))
            elif candidate:
                hit = candidate in proposed_names
            else:
                reasons.append("Cannot identify a registered shortcut: " + bind.get("description", "Unknown"))
                continue
            own = (bind.get("description") == "Herdr Shell: " + action
                   and bind.get("dispatcher", "__lua") == "__lua" and hit)
            if hit and not own:
                reasons.append(bind.get("description") or "Existing desktop shortcut")
        if reasons:
            conflicts[action] = "; ".join(dict.fromkeys(reasons))
    return conflicts
