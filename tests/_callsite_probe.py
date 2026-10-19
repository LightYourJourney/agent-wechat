"""callsite 绑定探针：两个同 API 调用点，行号固定。"""


def hit(p):
    p.write_text("x")


def miss(p):
    p.write_text("y")
