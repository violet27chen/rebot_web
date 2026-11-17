import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
from backend.leader_connection import verify_servos, initial_positions

PKG=Path('/home/legion/miniconda3/envs/lerobot/lib/python3.12/site-packages/lerobot_teleoperator_rebot_arm_102')
RECORD=Path('/home/legion/rebot_lerobot/lerobot/src/lerobot/scripts/lerobot_record.py')


class ConnectionTests(unittest.TestCase):
    def test_transient_ping_is_retried_and_no_commands_are_written(self):
        bus=Mock();bus.ping.side_effect=[False,True,True]
        verify_servos(bus,{'a':0,'b':1},'/dev/qa',1000000,sleep=lambda _:None)
        self.assertEqual([call.args for call in bus.ping.call_args_list],[(0,),(0,),(1,)])
        self.assertEqual({call[0] for call in bus.mock_calls},{'ping'})
    def test_offline_servo_reports_port_and_all_missing_ids_with_bounded_calls(self):
        bus=Mock();bus.ping.side_effect=lambda sid:sid==1
        with self.assertRaisesRegex(RuntimeError,r'/dev/qa.*响应 ID：1.*a\(ID 0\).*c\(ID 2\)'):
            verify_servos(bus,{'a':0,'b':1,'c':2},'/dev/qa',1000000,sleep=lambda _:None)
        self.assertEqual(bus.ping.call_count,9)
    def test_real_initial_angles_are_required_instead_of_zero_fallback(self):
        read=Mock(side_effect=[RuntimeError('cold start'),{'a':12.,'b':-8.}])
        self.assertEqual(initial_positions(read,['a','b'],sleep=lambda _:None),{'a':12.,'b':-8.})
        for value in [{},{'a':float('nan'),'b':0}]:
            with self.assertRaisesRegex(RuntimeError,'未使用默认零值'):
                initial_positions(lambda:value,['a','b'],sleep=lambda _:None)


@unittest.skipUnless(PKG.exists(),'Leader plugin not installed')
class PluginTests(unittest.TestCase):
    def extract(self,name,cls,method,**scope):
        tree=ast.parse((PKG/name).read_text())
        source=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==cls)
        func=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name==method)
        new=ast.ClassDef(name='TestPlugin',bases=[],keywords=[],decorator_list=[],body=[func])
        env={'logger':Mock(),'DeviceAlreadyConnectedError':RuntimeError,**scope}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[new],type_ignores=[])),name,'exec'),env)
        return env['TestPlugin']
    def test_partial_leader_connection_releases_left_and_retains_right_error(self):
        C=self.extract('bi_rebot_arm_102_leader.py','BiRebotArm102Leader','connect')
        left=NS(is_connected=False);right=NS(is_connected=False)
        def connect(_):left.is_connected=True
        left.connect=connect;left.disconnect=Mock(side_effect=lambda:setattr(left,'is_connected',False))
        right.connect=Mock(side_effect=RuntimeError('right servo offline'));right.disconnect=Mock()
        obj=C();obj.left_arm=left;obj.right_arm=right
        with self.assertRaisesRegex(RuntimeError,'right servo offline'):obj.connect()
        left.disconnect.assert_called_once();right.disconnect.assert_not_called()
    def test_single_leader_failure_closes_bus_without_configure_or_zero_seed(self):
        bus=Mock();verify=Mock(side_effect=RuntimeError('no reply'))
        C=self.extract('rebot_arm_102_leader.py','RebotArm102Leader','connect',FashionStarServo=Mock(return_value=bus),verify_servos=verify,initial_positions=Mock())
        obj=C();obj.is_connected=False;obj._last_raw_positions={'old':123};obj.config=NS(port='/dev/qa',baudrate=1000000,joint_ids={'a':0});obj.configure=Mock()
        with self.assertRaisesRegex(RuntimeError,'no reply'):obj.connect()
        bus.close.assert_called_once();obj.configure.assert_not_called();self.assertIsNone(obj.bus);self.assertEqual(obj._last_raw_positions,{})
    def test_recording_connects_leaders_before_followers(self):
        tree=ast.parse(RECORD.read_text());func=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='record')
        body=next(n for n in func.body if isinstance(n,ast.Try)).body
        follower=next(i for i,n in enumerate(body) if isinstance(n,ast.Expr) and ast.unparse(n)=='robot.connect()')
        leader=next(i for i,n in enumerate(body) if isinstance(n,ast.If) and any('teleop.connect()'==ast.unparse(stmt) for stmt in n.body))
        self.assertLess(leader,follower)


if __name__=='__main__':unittest.main()
