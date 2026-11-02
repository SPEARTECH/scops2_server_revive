@echo off
rem Round 33: typed 33, refusal for missing room
set SCCT_CREATE_FULL=1
set SCCT_CLOSE_ON_MEMBER_LEAVE=0
set SCCT_ROOM_MEMBER_LEAVE=1
set SCCT_HOST_JOIN_REPLY=ip
set SCCT_NEW_GROUP_TO_CREATOR=1
set SCCT_JOIN_GROUPINFO=1
set SCCT_R8=single
set SCCT_CLOSE_ON_LOBBY_LEAVE=0
set SCCT_R9=none
set SCCT_9FORM=ref
call "%~dp0run_test.bat"
