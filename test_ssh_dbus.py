import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("einstein.tail8df0f6.ts.net", username="root", password="FRANKSCERBO*", timeout=15)
stdin, stdout, stderr = ssh.exec_command("dbus -y | grep victronenergy")
print(stdout.read().decode())
ssh.close()
