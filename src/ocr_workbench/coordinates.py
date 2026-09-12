"""Explicit homogeneous transforms; all UI geometry is top-left pixel space.

PDF coordinates use points with a bottom-left origin before /Rotate. No geometry
is inferred by dividing a table into a uniform grid.
"""

import math

IDENTITY = [1., 0., 0., 0., 1., 0., 0., 0., 1.]


def matrix(value):
    if isinstance(value, (list, tuple)) and len(value) == 3:
        value = [item for row in value for item in row]
    if not isinstance(value, (list, tuple)) or len(value) != 9:
        raise ValueError("坐标变换必须为 3×3 矩阵")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in value):
        raise ValueError("坐标变换包含无效数值")
    return list(map(float, value))


def multiply(after, before):
    a, b = matrix(after), matrix(before)
    return [sum(a[r * 3 + k] * b[k * 3 + c] for k in range(3))
            for r in range(3) for c in range(3)]


def inverse(value):
    a, b, c, d, e, f, g, h, i = matrix(value)
    adj = [e*i-f*h, c*h-b*i, b*f-c*e, f*g-d*i, a*i-c*g, c*d-a*f,
           d*h-e*g, b*g-a*h, a*e-b*d]
    det = a*adj[0] + b*adj[3] + c*adj[6]
    if abs(det) < 1e-12:
        raise ValueError("坐标变换不可逆，请重新定位")
    return [v / det for v in adj]


def point(transform, xy):
    a = matrix(transform)
    if len(xy) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) for v in xy):
        raise ValueError("坐标无效")
    x, y = xy
    z = a[6]*x + a[7]*y + a[8]
    if abs(z) < 1e-12:
        raise ValueError("坐标映射至无穷远，请重新定位")
    return [(a[0]*x+a[1]*y+a[2])/z, (a[3]*x+a[4]*y+a[5])/z]


def polygon(transform, points):
    return [point(transform, xy) for xy in points]


def box_polygon(box):
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValueError("区域必须有四个边界坐标")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in box):
        raise ValueError("区域包含无效数值")
    x0, y0, x1, y1 = box
    if x0 >= x1 or y0 >= y1:
        raise ValueError("区域面积必须大于零")
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def bounds(points):
    return [min(x for x, _ in points), min(y for _, y in points),
            max(x for x, _ in points), max(y for _, y in points)]


def validate_polygon(points, width, height):
    if not isinstance(points, list) or not 3 <= len(points) <= 16:
        raise ValueError("定位区域必须为 3–16 个点的多边形")
    for xy in points:
        point(IDENTITY, xy)
        if not (0 <= xy[0] <= width and 0 <= xy[1] <= height):
            raise ValueError("定位区域超出图像版本边界")
    area = abs(sum(points[n][0]*points[(n+1) % len(points)][1]
                   - points[(n+1) % len(points)][0]*points[n][1] for n in range(len(points)))) / 2
    if area < 1:
        raise ValueError("定位区域面积太小")
    return points


def pdf_transform(crop_box, rotation=0, dpi=300, user_unit=1):
    """Map unrotated PDF user-space to the exact rendered CropBox viewport."""
    box_polygon(crop_box)
    if rotation not in (0, 90, 180, 270):
        raise ValueError("PDF 旋转必须是 90 度的倍数")
    if type(dpi) not in (int, float) or not math.isfinite(dpi) or not 36 <= dpi <= 1200:
        raise ValueError("渲染分辨率必须在 36–1200 DPI 之间")
    if type(user_unit) not in (int, float) or not math.isfinite(user_unit) or user_unit <= 0:
        raise ValueError("PDF UserUnit 无效")
    x0, y0, x1, y1 = crop_box
    scale = dpi / 72 * user_unit
    w, h = (x1-x0)*scale, (y1-y0)*scale
    base = [scale, 0, -x0*scale, 0, -scale, y1*scale, 0, 0, 1]
    if rotation == 90:
        base = multiply([0, -1, h, 1, 0, 0, 0, 0, 1], base)
    elif rotation == 180:
        base = multiply([-1, 0, w, 0, -1, h, 0, 0, 1], base)
    elif rotation == 270:
        base = multiply([0, 1, 0, -1, 0, w, 0, 0, 1], base)
    return base


def operation_transform(operation, width, height):
    """Return parent->child mapping; None explicitly invalidates nonlinear maps."""
    kind = operation.get("kind")
    if kind in ("contrast", "import", "pdf-render", "tiff-frame"):
        return IDENTITY[:]
    if kind == "crop":
        x, y, _, _ = map(round, operation["box"])
        return [1, 0, -x, 0, 1, -y, 0, 0, 1]
    if kind == "perspective":
        return matrix(operation["matrix"])
    if kind == "rotate":
        if operation.get("degrees") not in (0, 90, 180, 270, -90):
            return None
        degrees = operation["degrees"] % 360
        return {0: IDENTITY[:], 90: [0, -1, height, 1, 0, 0, 0, 0, 1],
                180: [-1, 0, width, 0, -1, height, 0, 0, 1],
                270: [0, 1, 0, -1, 0, width, 0, 0, 1]}[degrees]
    if kind == "batch":
        result = IDENTITY[:]
        for op in operation["preset"]:
            next_matrix = operation_transform(op, width, height)
            if next_matrix is None:
                return None
            result = multiply(next_matrix, result)
            if op["kind"] == "rotate" and op["degrees"] % 180:
                width, height = height, width
        return result
    return None
