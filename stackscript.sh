## stand up server & login
# create lightsail ubuntu instance  ($12/mo)
# download ssh key and set permissions
# (windows) icacls <pem path> /inheritance:r
# (windows) icacls <pem path> /grant:r "$($env:USERNAME):R"
# 
## setup server environment
# replace ip in gsinit_diag_weblocal.php
# create pat token
# allow for repos and all perms
# git clone <repo>
# enter username when prompted
# enter PAT for password when prompted
# git add .
# git commit -m "message"
# git push 
#
## setup server stack
sudo systemctl stop systemd-resolved
sudo systemctl disable systemd-resolved
sudo apt-get install --update
sudo apt-get install --upgrade
sudo apt-get install python3-pip
sudo apt install python3.12-venv
python3 -m venv venv
cd venv/bin
source activate
cd ../..
python3 -m pip install -r requirements.txt
cd scops2_server_revive
sudo chmod 655 run.sh



## Kill everything and restart clean example
# sudo pkill -f ubigs_router
# sudo pkill -f gs_http_server
# sudo pkill -f dns_override
# sudo pkill -f udp_reply
# sudo pkill -f udp_log
# sudo pkill -f tcp_log
# sudo pkill -f s_server
# sleep 1
#
## run server
# sudo env "PATH=$PWD/venv/bin:$PATH" ./run.sh