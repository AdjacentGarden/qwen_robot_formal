from concurrent.futures import ThreadPoolExecutor
import pytest
from robot_graph.cloud_budget import CloudBudget


def test_budget_never_exceeds_limit_even_concurrently(tmp_path):
    budget=CloudBudget(tmp_path/'budget.db',limit=3)
    def reserve(_):
        try:return budget.reserve('test')
        except RuntimeError:return None
    with ThreadPoolExecutor(max_workers=8) as pool:values=list(pool.map(reserve,range(20)))
    assert len([v for v in values if v is not None])==3
    assert budget.status()['used']==3
    with pytest.raises(RuntimeError):CloudBudget(tmp_path/'budget.db',limit=3).reserve('again')
