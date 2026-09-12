import threading
import httpx

from robot_graph.dialogue import Dialogue
from robot_graph.execution import SimulatedBackend
from robot_graph.runtime import Runtime
from robot_graph.server import create_server


def test_api_routes_and_errors(tmp_path):
    driver=SimulatedBackend();rt=Runtime(tmp_path,driver)
    server=create_server(rt,Dialogue(rt),0)
    thread=threading.Thread(target=server.serve_forever);thread.start()
    url='http://127.0.0.1:'+str(server.server_port)
    try:
        assert httpx.get(url+'/health').json()['base_locked']
        assert httpx.get(url+'/').status_code==200
        r=httpx.post(url+'/tasks',json={'workflow':'meeting'})
        assert r.status_code==400 and 'base_motion_forbidden' in r.json()['error']
        r=httpx.post(url+'/turn',json={'text':'原地会议投影','request_id':'1'})
        assert r.status_code==200
        task=r.json()['task_id']
        assert httpx.post(url+'/turn',json={'text':'原地会议投影','request_id':'1'}).json()['task_id']==task
        assert httpx.get(url+'/task',params={'id':task,'session':'other'}).status_code==404
        assert httpx.post(url+'/control',json={'task_id':task,'command':'cancel'},headers={'Origin':'https://evil.invalid'}).status_code==403
        assert httpx.post(url+'/control',json={'task_id':task,'command':'cancel'}).status_code==200
        rt.tick_all();assert not driver.calls
    finally:
        server.shutdown();thread.join();server.server_close();rt.close()


def test_large_and_malformed_request_rejected(tmp_path):
    rt=Runtime(tmp_path,SimulatedBackend());server=create_server(rt,Dialogue(rt),0)
    thread=threading.Thread(target=server.serve_forever);thread.start();url='http://127.0.0.1:'+str(server.server_port)
    try:
        assert httpx.post(url+'/turn',content=b'{').status_code==400
        assert httpx.post(url+'/turn',json={'text':'a'*70000}).status_code==413
        assert httpx.post(url+'/tasks',json={'action':{'kind':'base.move','args':{'direction':'forward'}}}).status_code==400
        assert not rt.store.all('SELECT * FROM operations')
    finally:server.shutdown();thread.join();server.server_close();rt.close()
