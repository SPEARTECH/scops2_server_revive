@echo off
set SCCT_ACK42=1
set SCCT_LOBBY_CONFIG=0x1C0
set SCCT_PUSH_ROOMS_AFTER_42=1
set SCCT_NEW_GROUP=room
set SCCT_CREATE_ACK=roomid
set SCCT_ROOM_CONFIG=0x1D8
call "%~dp0run.bat"
