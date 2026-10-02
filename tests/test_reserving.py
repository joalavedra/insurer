from insurer.reserving import build_triangle


def triangle_claim(origin, dev, amount):
    year, month = (int(part) for part in origin.split("-"))
    origin_index = year * 12 + month - 1
    report_index = origin_index + dev
    report_year, report_month = divmod(report_index, 12)
    return {
        "loss_date": f"{origin}-01",
        "notified_date": f"{report_year:04d}-{report_month + 1:02d}-15",
        "incurred_cents": amount,
    }


def test_known_triangle_reproduces_chain_ladder_ldfs():
    claims = [
        triangle_claim("2021-11", 0, 100),
        triangle_claim("2021-11", 1, 100),
        triangle_claim("2021-11", 2, 100),
        triangle_claim("2021-12", 0, 200),
        triangle_claim("2021-12", 1, 200),
        triangle_claim("2022-01", 0, 300),
    ]
    result = build_triangle(claims, "2022-01-31")
    assert result["origins"] == ["2021-11", "2021-12", "2022-01"]
    assert result["values_cents"] == [[100, 200, 300], [200, 400], [300]]
    assert abs(result["ldf"][0] - 2.0) < 1e-8
    assert abs(result["ldf"][1] - 1.5) < 1e-8


def test_fewer_than_three_origins_returns_zero_ibnr_and_note():
    claims = [
        triangle_claim("2027-01", 0, 500),
        triangle_claim("2027-02", 0, 600),
    ]
    result = build_triangle(claims, "2027-03-31")
    assert result["ibnr_cents"] == [0, 0]
    assert result["note"] is not None
