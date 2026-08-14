echo "====== 创建 sub_discover.service======"
sudo tee /etc/systemd/system/multi_process_controller.service > /dev/null <<'EOF'
[Unit]
Description=multi_process_controller Service
After=network.target

[Service]
Type=simple
User=jshalzx
Group=jshalzx
WorkingDirectory=/home/jshalzx/shuqi888/AIE_SERVER
Environment="PATH=/home/jshalzx/ls/envs/py311/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart=/home/jshalzx/ls/envs/py311/bin/python /home/jshalzx/shuqi888/AIE_SERVER/api/multi_process_controller.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF