from app.agent.tools import normalize_order_id


def test_order_id_normalisation():
    for raw in ["10095", "#10095", "ord 10095", "ORD10095", "ORD-10095", " ord-10095 "]:
        assert normalize_order_id(raw) == "ORD-10095"
