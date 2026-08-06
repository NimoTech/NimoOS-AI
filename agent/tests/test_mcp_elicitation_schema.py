"""requestedSchema 渲染与答案校验。

这个模块存在的唯一理由是 SDK 不做这件事：mcp 2.0 把
ElicitRequestFormParams.requested_schema 标注成裸 dict[str, Any]
（types.ElicitRequestedSchema 就是这个别名），零建模零校验，而规范说
"Clients SHOULD validate all responses against the provided schema"。
"""
import pytest

from mcp_client.elicitation_schema import render_fields, validate_content


# ── 渲染 ──────────────────────────────────────────────────────────────────────

def test_plain_string_field():
    fields = render_fields({
        "type": "object",
        "properties": {"name": {"type": "string", "title": "Your name",
                                "description": "as it appears on the account",
                                "minLength": 2, "maxLength": 40}},
        "required": ["name"]})
    assert len(fields) == 1
    f = fields[0]
    assert f["key"] == "name" and f["type"] == "string"
    assert f["title"] == "Your name"
    assert f["description"] == "as it appears on the account"
    assert f["required"] is True
    assert (f["min_length"], f["max_length"]) == (2, 40)


def test_title_falls_back_to_key_and_description_to_empty():
    f = render_fields({"properties": {"tok": {"type": "string"}}})[0]
    assert f["title"] == "tok" and f["description"] == "" and f["required"] is False


def test_default_is_carried_through():
    """规范：Clients that support defaults SHOULD pre-populate form fields with them."""
    f = render_fields({"properties": {"n": {"type": "integer", "default": 7}}})[0]
    assert f["default"] == 7


def test_titled_enum_via_oneOf_is_a_choice_not_a_text_box():
    """这是"按 type 出控件"会真出 bug 的地方：oneOf 分支没有 type 键,
    天真实现会把它当自由文本框,于是服务端收到的是标题而不是 const 值。"""
    f = render_fields({"properties": {"plan": {"oneOf": [
        {"const": "free", "title": "Free tier"},
        {"const": "pro", "title": "Pro tier"}]}}})[0]
    assert f["type"] == "enum"
    assert f["options"] == [{"value": "free", "title": "Free tier"},
                            {"value": "pro", "title": "Pro tier"}]


def test_titled_enum_via_anyOf_too():
    f = render_fields({"properties": {"p": {"anyOf": [{"const": 1, "title": "One"}]}}})[0]
    assert f["type"] == "enum" and f["options"] == [{"value": 1, "title": "One"}]


@pytest.mark.parametrize("node", [
    {"oneOf": [{"const": {"a": 1}, "title": "Weird"}]},
    {"anyOf": [{"const": ["a", "b"], "title": "Weird"}]},
    {"enum": [{"a": 1}]},
    {"enum": [["a"]]},
])
def test_a_non_scalar_const_never_becomes_a_selectable_option(node):
    """ElicitResult.content 是 dict[str, str|int|float|bool|list[str]|None] —— pydantic
    拒 dict 和 list 值。而 _validate_one 的 enum 规则是纯成员判断,所以**从 schema 里
    出来的**值回填时必定通过校验。于是一个 {"const": {"a": 1}} 会一路绿灯走到
    ElicitResult(action="accept", content=...) 才炸,而那已经在回调里面了：
    抛出去就是 _dispatch_all 的 ExceptionGroup,同一轮别的卡全部陪葬。

    候选项没了就退化成自由文本框（本模块一贯的"宁可松,不可丢字段"）。"""
    f = render_fields({"properties": {"p": node}})[0]
    assert f["type"] == "string", "候选全被丢掉后必须退化成自由文本,而不是空下拉"
    assert f["options"] is None


def test_a_non_scalar_const_alongside_good_ones_only_drops_itself():
    f = render_fields({"properties": {"p": {"oneOf": [
        {"const": "free", "title": "Free"},
        {"const": {"a": 1}, "title": "Weird"},
        {"const": 2, "title": "Two"}]}}})[0]
    assert f["type"] == "enum"
    assert f["options"] == [{"value": "free", "title": "Free"},
                            {"value": 2, "title": "Two"}]


def test_scalar_consts_keep_their_type_rather_than_being_stringified():
    """丢弃而不是 str() 化：数组分支 str() 是因为 content 把数组钉死成 list[str],
    标量字段没有这个约束 —— {"const": 1} 今天就是以整数 1 回到服务端的,把它变成
    "1" 是在服务端背后偷偷改值。"""
    f = render_fields({"properties": {"p": {"enum": [1, 2.5, True, "x"]}}})[0]
    assert [o["value"] for o in f["options"]] == [1, 2.5, True, "x"]


def test_bare_enum_uses_the_value_as_its_own_title():
    f = render_fields({"properties": {"c": {"type": "string", "enum": ["red", "blue"]}}})[0]
    assert f["type"] == "enum"
    assert f["options"] == [{"value": "red", "title": "red"},
                            {"value": "blue", "title": "blue"}]


def test_multi_select_via_array_items_anyOf():
    f = render_fields({"properties": {"scopes": {
        "type": "array", "minItems": 1, "maxItems": 2,
        "items": {"anyOf": [{"const": "read", "title": "Read"},
                            {"const": "write", "title": "Write"}]}}}})[0]
    assert f["type"] == "multi_enum"
    assert f["options"] == [{"value": "read", "title": "Read"},
                            {"value": "write", "title": "Write"}]
    assert (f["min_items"], f["max_items"]) == (1, 2)


def test_multi_select_via_array_items_enum():
    f = render_fields({"properties": {"s": {"type": "array",
                                            "items": {"enum": ["a", "b"]}}}})[0]
    assert f["type"] == "multi_enum"
    assert [o["value"] for o in f["options"]] == ["a", "b"]


def test_array_options_are_stringified_because_content_pins_arrays_to_list_of_str():
    """ElicitResult.content 的标注是 dict[str, str|int|float|bool|list[str]|None]
    —— 实测 pydantic 连 list[int] 都拒。所以数组字段的候选值只能以字符串形态存在,
    这里就地 str() 掉,校验时也按字符串比。非字符串 const 的数组在协议层不可表达。"""
    f = render_fields({"properties": {"n": {"type": "array",
                                            "items": {"enum": [1, 2]}}}})[0]
    assert [o["value"] for o in f["options"]] == ["1", "2"]
    assert [o["title"] for o in f["options"]] == ["1", "2"]


def test_numeric_and_boolean_fields():
    fields = {f["key"]: f for f in render_fields({"properties": {
        "age": {"type": "integer", "minimum": 0, "maximum": 130},
        "ratio": {"type": "number", "minimum": 0.0},
        "ok": {"type": "boolean"}}})}
    assert fields["age"]["type"] == "integer" and fields["age"]["maximum"] == 130
    assert fields["ratio"]["type"] == "number" and fields["ratio"]["maximum"] is None
    assert fields["ok"]["type"] == "boolean"


def test_format_is_kept_only_for_the_four_the_spec_names():
    fields = {f["key"]: f for f in render_fields({"properties": {
        "a": {"type": "string", "format": "email"},
        "b": {"type": "string", "format": "uri"},
        "c": {"type": "string", "format": "date"},
        "d": {"type": "string", "format": "date-time"},
        "e": {"type": "string", "format": "ipv6"}}})}
    assert [fields[k]["format"] for k in "abcd"] == ["email", "uri", "date", "date-time"]
    assert fields["e"]["format"] is None      # 不认识的 format 不假装支持


def test_unknown_type_degrades_to_a_text_box_instead_of_disappearing():
    """未知/缺失 type 时给自由文本框,用户至少还能作答;丢掉字段会让整张卡无法满足
    required,用户被卡死且完全不知道为什么。"""
    fields = {f["key"]: f for f in render_fields({"properties": {
        "x": {"type": "null"}, "y": {}, "z": {"type": "object"}}})}
    assert {f["type"] for f in fields.values()} == {"string"}


def test_malformed_schemas_never_raise():
    assert render_fields({}) == []
    assert render_fields({"properties": None}) == []
    assert render_fields(None) == []
    assert render_fields({"properties": {"a": "not-a-dict"}})[0]["type"] == "string"
    # required 不是 list 时不得炸,按"都不必填"处理
    assert render_fields({"properties": {"a": {}}, "required": "a"})[0]["required"] is False


# ── 校验 ──────────────────────────────────────────────────────────────────────

SCHEMA = {
    "type": "object",
    "properties": {
        "name":  {"type": "string", "minLength": 2, "maxLength": 5},
        "email": {"type": "string", "format": "email"},
        "age":   {"type": "integer", "minimum": 0, "maximum": 130},
        "ok":    {"type": "boolean"},
        "plan":  {"oneOf": [{"const": "free", "title": "F"}, {"const": "pro", "title": "P"}]},
        "scopes": {"type": "array", "minItems": 1, "maxItems": 2,
                   "items": {"enum": ["read", "write", "admin"]}},
    },
    "required": ["name"],
}


def test_a_good_answer_validates():
    assert validate_content(SCHEMA, {
        "name": "Nimo", "email": "a@b.co", "age": 3, "ok": True,
        "plan": "pro", "scopes": ["read"]}) is None


def test_optional_fields_may_be_absent_or_null():
    assert validate_content(SCHEMA, {"name": "Nimo"}) is None
    assert validate_content(SCHEMA, {"name": "Nimo", "age": None}) is None


@pytest.mark.parametrize("content", [
    {},                                              # required 缺失
    {"name": None},                                  # required 显式为 null
    {"name": "N"},                                   # minLength
    {"name": "Nimoooo"},                             # maxLength
    {"name": "Nimo", "email": "not-an-email"},
    {"name": "Nimo", "age": "3"},                    # 字符串不是 integer
    {"name": "Nimo", "age": 3.5},                    # 小数不是 integer
    {"name": "Nimo", "age": 999},                    # maximum
    {"name": "Nimo", "ok": "yes"},                   # 字符串不是 boolean
    {"name": "Nimo", "plan": "F"},                   # 送了 title 而不是 const
    {"name": "Nimo", "plan": "enterprise"},          # 不在候选里
    {"name": "Nimo", "scopes": "read"},              # 不是 list
    {"name": "Nimo", "scopes": []},                  # minItems
    {"name": "Nimo", "scopes": ["read", "write", "admin"]},   # maxItems
    {"name": "Nimo", "scopes": ["root"]},            # 不在候选里
    {"name": "Nimo", "surprise": "x"},               # schema 里没有的字段
])
def test_bad_answers_are_rejected_with_a_reason(content):
    err = validate_content(SCHEMA, content)
    assert isinstance(err, str) and err


def test_bool_is_not_accepted_as_a_number():
    """Python 里 isinstance(True, int) 为真 —— 不显式排掉,True 会被当成 age=1
    悄悄发给服务端。"""
    assert validate_content(SCHEMA, {"name": "Nimo", "age": True}) is not None


def test_non_dict_content_is_rejected():
    assert validate_content(SCHEMA, None) is not None
    assert validate_content(SCHEMA, ["name"]) is not None


# ── 与浏览器原生约束的对齐（前端不再重写规则,只映射成 DOM 属性） ──────────────

def test_email_rule_is_the_whatwg_one_the_browser_enforces():
    """卡片把 email 字段渲染成 <input type="email">,浏览器执行的就是 WHATWG 那条
    正则。这里若写一条更严的（比如要求域名带点）,浏览器会放行 `a@b` 而这里拒 ——
    用户看不到这个拒绝要来,也无从预防。两边一致必须是**构造出来的**,不是靠人记得同步。

    直接钉正则源码而不是只钉几个样例：样例通不过的组合太多,漏一个就等于没钉。
    """
    from mcp_client import elicitation_schema as es

    assert es._EMAIL_RE.pattern == (
        r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+"
        r"@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
        r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$")

    # 浏览器接受、我们也必须接受的形状（这条正是"自己写更严"会踩的）
    assert validate_content({"properties": {"e": {"type": "string", "format": "email"}}},
                            {"e": "a@b"}) is None
    # 浏览器拒绝、我们也拒绝
    for bad in ("nope", "a@", "@b.co", "a b@c.co"):
        assert validate_content({"properties": {"e": {"type": "string", "format": "email"}}},
                                {"e": bad}) is not None


@pytest.mark.parametrize("fmt,emitted", [
    ("date", "2026-08-05"),              # <input type="date"> 的输出形状
    ("date-time", "2026-08-05T14:30"),   # <input type="datetime-local"> 的输出形状
    ("date-time", "2026-08-05T14:30:00"),
])
def test_date_rules_accept_what_the_native_controls_emit(fmt, emitted):
    """这两种字段用原生控件渲染,用户根本打不出畸形值 —— 但只有这条测试能保证
    "控件产得出的东西这里一定收"。改窄了任何一条,用户会卡在一个填得完全正确的表单上。"""
    assert validate_content(
        {"properties": {"d": {"type": "string", "format": fmt}}}, {"d": emitted}) is None


def test_uri_is_the_one_format_with_no_native_backstop():
    """<input type="url"> 比这条规则更严,用它会让我们放行的值在浏览器里根本提交不了。
    所以 uri 字段渲染成纯文本框,这里是它唯一的关卡 —— 被拒时靠回调的重问循环
    把原因带回卡片。"""
    schema = {"properties": {"u": {"type": "string", "format": "uri"}}}
    assert validate_content(schema, {"u": "https://a.example/x"}) is None
    assert validate_content(schema, {"u": "mailto:a@b"}) is None      # type=url 会拒这个
    assert validate_content(schema, {"u": "not a uri"}) is not None


# 覆盖到每一种描述符类型,外加几种"服务端可以合法塞进 schema 的怪东西"。
#
# 这条测试的名字断言的是一条**全称命题**（"every valid answer"）,但它原先只查了一个
# 手挑的 content —— 于是 Critical 2 活着的时候它照样是绿的：一个 {"const": {"a": 1}}
# 的 oneOf 通过了 validate_content,却在 ElicitResult 上抛 ValidationError。名字
# 承诺的性质必须由用例覆盖来兑现,否则它就只是一句祝愿。
_ROUND_TRIP_CASES = [
    ("string",        {"n": {"type": "string"}},                     {"n": "Nimo"}),
    ("string-format", {"n": {"type": "string", "format": "email"}},  {"n": "a@b.co"}),
    ("integer",       {"n": {"type": "integer"}},                    {"n": 3}),
    ("number",        {"n": {"type": "number"}},                     {"n": 1.5}),
    ("boolean",       {"n": {"type": "boolean"}},                    {"n": True}),
    ("enum-str",      {"n": {"enum": ["a", "b"]}},                   {"n": "a"}),
    ("enum-int",      {"n": {"enum": [1, 2]}},                       {"n": 1}),
    ("enum-float",    {"n": {"enum": [1.5]}},                        {"n": 1.5}),
    ("enum-bool",     {"n": {"enum": [True, False]}},                {"n": True}),
    ("oneOf-const",   {"n": {"oneOf": [{"const": "pro", "title": "P"}]}}, {"n": "pro"}),
    ("multi_enum",    {"n": {"type": "array", "items": {"enum": ["r", "w"]}}},
                      {"n": ["r", "w"]}),
    ("multi_enum-nonstr-const",
                      {"n": {"type": "array", "items": {"enum": [1, 2]}}}, {"n": ["1"]}),
    ("unknown-type-degrades", {"n": {"type": "object"}},             {"n": "text"}),
    # Critical 2 的原样复现：非标量 const。渲染时被丢掉 -> 字段退化成自由文本 ->
    # 那个 dict 再也不是"被提供的候选",校验会拒它,于是永远走不到 ElicitResult。
    ("non-scalar-const-degrades",
                      {"n": {"oneOf": [{"const": {"a": 1}, "title": "W"}]}}, {"n": "a"}),
    ("optional-null", {"n": {"type": "string"}},                     {"n": None}),
    ("empty",         {"n": {"type": "string"}},                     {}),
]


@pytest.mark.parametrize("props,content",
                         [(p, c) for _, p, c in _ROUND_TRIP_CASES],
                         ids=[i for i, _, _ in _ROUND_TRIP_CASES])
def test_every_valid_answer_survives_the_real_ElicitResult_model(props, content):
    """校验器放行的东西必须真能构造出 ElicitResult —— 否则我们只是把
    ValidationError 从校验时挪到了发送时,而那时用户的答案已经没救了。
    跑在真实 SDK 模型上,不是鸭子类型假对象（交接文档 §8）。"""
    from mcp.types import ElicitResult

    schema = {"type": "object", "properties": props}
    assert validate_content(schema, content) is None
    assert ElicitResult(action="accept", content=content).content == content


def test_every_valid_answer_survives_it_for_the_full_mixed_schema():
    """单字段之外再钉一次组合形态 —— 原来那条手挑用例,保留。"""
    from mcp.types import ElicitResult

    content = {"name": "Nimo", "email": "a@b.co", "age": 3, "ok": True,
               "plan": "pro", "scopes": ["read", "write"]}
    assert validate_content(SCHEMA, content) is None
    assert ElicitResult(action="accept", content=content).content == content
