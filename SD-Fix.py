import subprocess
import secrets
print("Starting SillyTavern Extras")
extras_url = '(disabled)'
params = []
if use_cpu:
    params.append('--cpu')
if use_sd_cpu:
    params.append('--sd-cpu')
params.append('--port 6000')
params.append('--listen')
modules = []

if extras_enable_caption:
  modules.append('caption')
if extras_enable_sd:
  modules.append('sd')


if extras_enable_websearch:
    print("Enabling WebSearch module")
    modules.append('websearch')
    !apt update
    !apt install -y chromium-chromedriver

params.append(f'--captioning-model={captioning_model}')
params.append(f'--sd-model={sd_model}')
params.append(f'--enable-modules={",".join(modules)}')

cd /
git clone https://github.com/Abdulhanan535/SillyTavern-ExtrasFix
%cd /SillyTavern-ExtrasFix
git clone https://github.com/Cohee1207/tts_samples
npm install -g localtunnel
pip install -r requirements.txt

# Generate a random API key
api_key = secrets.token_hex(5)

# Write the API key to api_key.txt
with open('./api_key.txt', 'w') as f:
    f.write(api_key)
print(f"API Key generated: {api_key}")

cmd = f"python server.py {' '.join(params)}"
print(cmd)


extras_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd='/SillyTavern-ExtrasFix', shell=True)
