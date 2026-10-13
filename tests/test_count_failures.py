import pytest

from omt.count_failures import count_failures

class TestCountFailures:

    '''pytest
    input: {}
    expect: {}
    '''
    def test_empty_list(self):
        assert count_failures([]) == {}

    '''pytest
    input: [
        {"tool": "read", "ok": True},
        {"tool": "write", "ok": True},
        {"tool": "write", "ok": True},
        {"tool": "write", "ok": True},
    ]
    expect: {}
    '''
    def test_full_true(self):
        records = [
            {"tool": "read", "ok": True},
            {"tool": "write", "ok": True},
            {"tool": "write", "ok": True},
            {"tool": "write", "ok": True},
        ]

        assert count_failures(records=records) == {}


    '''pytest
    input = [
            {"tool": "read", "ok": True},
            {"tool": "write", "ok": True},
            {"tool": "write", "ok": True},
            {"tool": "write", "ok": False},
        ]
    except: {"write": 1}
    '''
    def test_one_false(self):
        records = [
            {"tool": "read", "ok": True},
            {"tool": "write", "ok": True},
            {"tool": "write", "ok": True},
            {"tool": "write", "ok": False},
        ] 

        assert count_failures(records=records) == {"write": 1}

    
    '''pytest
    input = [
            {"tool": "read", "ok": True},
            {"tool": "write", "ok": False},
            {"tool": "write", "ok": True},
            {"tool": "write", "ok": False},
        ]
    except: {"write": 2}
    '''
    def test_repeat_false(self):
        records = [
            {"tool": "read", "ok": True},
            {"tool": "write", "ok": False},
            {"tool": "write", "ok": True},
            {"tool": "write", "ok": False},
        ] 

        assert count_failures(records=records) == {"write": 2}


    '''pytest
    input = [
            {"tool": "read", "ok": False},
            {"tool": "write", "ok": True},
            {"tool": "edit", "ok": True},
            {"tool": "write", "ok": False},
            {"tool": "edit", "ok": False},
        ] 
    except: {"read": 1, "write": 1, "edit": 1}
    '''
    def test_multi_tool_false(self):
        records = [
            {"tool": "read", "ok": False},
            {"tool": "write", "ok": True},
            {"tool": "edit", "ok": True},
            {"tool": "write", "ok": False},
            {"tool": "edit", "ok": False},
        ] 

        assert count_failures(records=records) == {"read": 1, "write": 1, "edit": 1}
