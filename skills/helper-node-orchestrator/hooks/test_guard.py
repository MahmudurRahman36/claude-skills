#!/usr/bin/env python3
"""Fixture tests for guard.py. Run: python -B hooks/test_guard.py (stdlib unittest; no network, ssh or docker).

Hosts come from fixtures/guard-view.sample.json: ha helper (192.0.2.11, alias haa, context orch-ha), hb helper+prod
(192.0.2.12:2222, alias hbx, context orch-hb), pc prod (192.0.2.20); project C:/work/shop with its store in
.git/orch, deploy ./scripts/deploy.sh, rollback `ssh hbx /srv/shop/rollback.sh` and a job index; project
C:/work/lab whose job index holds python and node tools (pytest, python -m pytest, ruff, npx jest, ...)."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import guard  # noqa: E402

SAMPLE = os.path.join(HERE, "fixtures", "guard-view.sample.json")
ORCH = guard.ORCH
ROOT = "C:/work/shop"
OUTSIDE = "C:/work/other"
B, P = "Bash", "PowerShell"


def sample():
    with open(SAMPLE, encoding="utf-8") as f:
        return json.load(f)

# (tool, command, rule) -> deny
DENY = [
    # (a) raw docker aimed at the helper-only host
    (B, "docker --context ha ps", "a"),
    (B, "docker --context=orch-ha images", "a"),
    (B, "docker -c haa ps", "a"),
    (B, "docker -H ssh://deploy@192.0.2.11:22 ps", "a"),
    (B, "docker --host=ssh://ha info", "a"),
    (B, "DOCKER_HOST=ssh://ha docker ps", "a"),
    (B, "env DOCKER_HOST=tcp://192.0.2.11:2375 docker ps", "a"),
    (B, "export DOCKER_HOST=ssh://deploy@192.0.2.11 && docker ps", "a"),
    (B, "DOCKER_CONTEXT=orch-ha docker build .", "a"),
    (B, "docker context use orch-ha", "a"),
    (B, "docker context create hx --docker host=ssh://deploy@192.0.2.11", "a"),
    (P, "$env:DOCKER_HOST='ssh://deploy@192.0.2.11'; docker ps", "a"),
    (P, "[Environment]::SetEnvironmentVariable('DOCKER_HOST','ssh://haa','User')", "a"),
    (P, "setx DOCKER_HOST ssh://haa", "a"),
    (P, "DOCKER --CONTEXT HA PS", "a"),
    (P, "& 'C:\\Program Files\\Docker\\Docker\\resources\\bin\\docker.exe' --context ha ps", "a"),
    (B, "sudo docker -H ssh://ha ps", "a"),
    # (b) ssh to the helper-only host
    (B, "ssh ha 'uptime; free -m; df -h; docker ps'", "b"),
    (B, "ssh -p 22 deploy@192.0.2.11 docker ps -a", "b"),
    (B, "ssh haa \"powershell -c 'docker images'\"", "b"),
    (B, "ssh ha 'sudo rm -rf /var/lib/docker/tmp'", "b"),
    (B, "ssh ha 'kill -9 1234'", "b"),
    (B, "ssh ha sudo systemctl restart docker", "b"),
    (B, "ssh haa wsl --shutdown", "b"),
    (B, "ssh deploy@192.0.2.11 sudo reboot", "b"),
    (B, "ssh haa 'shutdown /r /t 0'", "b"),
    (B, "ssh ha \"bash -c 'docker system df'\"", "b"),
    (B, "echo 'docker ps' | ssh ha", "b"),
    (B, "cat x.sh | ssh ha", "b"),
    (B, "ssh ha bash -s < cleanup.sh", "b"),
    (B, "ssh ha <<'EOF'\ndocker rm -f abc\nEOF", "b"),
    (B, "ssh ha 'cd /src && dotnet test'", "b"),
    (P, "ssh haa Stop-Process -Name dockerd", "b"),
    # (c) destructive docker anywhere
    (B, "docker system prune -af", "c"),
    (B, "docker image prune -a", "c"),
    (B, "docker image prune --all --force", "c"),
    (B, "docker image prune -fa", "c"),
    (B, "docker image prune -f", "c"),
    (B, "docker volume prune -f", "c"),
    (B, "docker builder prune -af", "c"),
    (B, "docker buildx prune --all", "c"),
    (B, "docker network prune", "c"),
    (B, "docker container prune -f", "c"),
    (B, "docker container prune --filter label=other=1", "c"),
    (B, "/usr/bin/docker system prune", "c"),
    (B, "ssh other-host docker volume prune -f", "c"),
    (B, "ssh pc docker system prune -af", "c"),
    (B, "bash -c 'docker system prune -f'", "c"),
    (B, "git status; docker system prune -f", "c"),
    (B, "echo hi && \\\n  docker system \\\n  prune -f", "c"),
    (P, "powershell -NoProfile -Command \"docker system prune -f\"", "c"),
    (P, "cmd /c \"docker volume prune -f\"", "c"),
    (B, "d\\ocker system prune -f", "c"),
    (B, "\"docker\" system prune", "c"),
    (B, "echo $(docker system prune -f)", "c"),
    (B, "timeout 60 nice -n 10 docker system prune -f", "c"),
    # (d) heavy work on the master (cwd inside the approved project root)
    (B, "docker build -t x .", "d"),
    (B, "docker buildx build --load -t x .", "d"),
    (B, "docker buildx --builder default build .", "d"),
    (B, "docker compose -f docker-compose.yml up -d", "d"),
    (B, "docker-compose up -d", "d"),
    (B, "docker run --rm -v $(pwd):/src -w /src mcr.microsoft.com/dotnet/sdk:9.0 dotnet test", "d"),
    (B, "docker run -it --rm node:22 npm ci", "d"),
    (B, "docker run --rm mcr.microsoft.com/playwright:v1.48.0 npx playwright test", "d"),
    (B, "dotnet test Shop.sln", "d"),
    (B, "dotnet build -c Release", "d"),
    (P, "dotnet.exe publish -c Release", "d"),
    (P, "DOTNET TEST", "d"),
    (B, "cd web && npm ci", "d"),
    (B, "npm install", "d"),
    (B, "npm i", "d"),
    (B, "npm test", "d"),
    (B, "npm run build", "d"),
    (B, "npm --prefix web run test", "d"),
    (P, "npm.cmd run build", "d"),
    (B, "npx playwright test", "d"),
    (B, "npx -y @playwright/test test", "d"),
    (B, "pnpm install", "d"),
    (B, "yarn build", "d"),
    (B, "yarn", "d"),
    (B, "sh -c 'cd api && dotnet test'", "d"),
    (P, "powershell -c \"cd web; npm test\"", "d"),
    (P, "cmd /c npm run build", "d"),
    (B, "sudo -E env FOO=1 dotnet test", "d"),
    (B, "wsl -e dotnet test", "d"),
    (B, "for d in a b; do docker build $d; done", "d"),
    (P, "1..2 | ForEach-Object { docker build . }", "d"),
    (P, "powershell -EncodedCommand ZABvAGMAawBlAHIAIABiAHUAaQBsAGQAIAAuAA==", "d"),
    (B, "eval \"dotnet test\"", "d"),
    # (e) flag file and env override
    (B, "touch ~/.helper-orch/ALLOW_LOCAL", "e"),
    (B, "echo 1 > ~/.helper-orch/ALLOW_LOCAL", "e"),
    (B, "cp /tmp/x ~/.helper-orch/ALLOW_LOCAL", "e"),
    (B, "mv ~/.helper-orch/x ~/.helper-orch/ALLOW_LOCAL", "e"),
    (B, "echo x | tee ~/.helper-orch/ALLOW_LOCAL", "e"),
    (B, "bash -c 'touch ~/.helper-orch/ALLOW_LOCAL'", "e"),
    (B, "python -c \"open('/home/m/.helper-orch/ALLOW_LOCAL','w')\"", "e"),
    (P, "New-Item -ItemType File ~/.helper-orch/ALLOW_LOCAL", "e"),
    (P, "Set-Content -Path $HOME\\.helper-orch\\ALLOW_LOCAL -Value 1", "e"),
    (P, "'' | Out-File ~/.helper-orch/allow_local", "e"),
    (P, "[IO.File]::WriteAllText(\"$HOME\\.helper-orch\\ALLOW_LOCAL\", '')", "e"),
    (B, "HELPER_ORCH_LOCAL=1 dotnet test", "e"),
    (B, "export HELPER_ORCH_LOCAL=1", "e"),
    (P, "$env:HELPER_ORCH_LOCAL = '1'", "e"),
    (P, "setx HELPER_ORCH_LOCAL 1", "e"),
    (B, "A=$(printf %s ALLOW_LOCAL); touch ~/.helper-orch/$A", "e"),
    (B, "cp /tmp/x ~/.helper-orch/ALLOW_L*", "e"),
    (B, "touch ~/.helper-orch/ALLOW_LOC''AL", "e"),
    # guard.json is user-only: no writes, and no deletes (that would drop the user's stricter prefs)
    (B, "echo '{\"new_heavy\":\"warn\"}' > ~/.helper-orch/guard.json", "e"),
    (B, "cp x ~/.helper-orch/guard.json", "e"),
    (P, "Set-Content ~/.helper-orch/guard.json '{}'", "e"),
    (B, "rm ~/.helper-orch/guard.json", "e"),
    (B, "mv ~/.helper-orch/guard.json /tmp/g.json", "e"),
    # more evasion forms
    (B, "/usr/bin/docker --context ha ps", "a"),
    (P, "docker.exe build .", "d"),
    (B, "sudo /usr/bin/docker build .", "d"),
    (B, "(cd api && dotnet test)", "d"),
    (B, "x=$(ssh ha docker ps)", "b"),
    (B, "ssh ha 'echo $(docker ps)'", "b"),
    (B, "nohup docker compose up -d &", "d"),
    (B, "xargs -n1 docker build < dirs", "d"),
    (B, "docker \\\nbuild .", "d"),
    (B, "doc\\\nker build .", "d"),
    (B, "dotnet run --project api", "d"),
    (B, "dotnet pack -c Release", "d"),
    (B, "dotnet restore", "d"),
    (B, "dotnet msbuild Shop.sln", "d"),
    (B, "dotnet vstest x.Tests.dll", "d"),
    (B, "dotnet watch test", "d"),
    (P, "Start-Process dotnet -ArgumentList test -Wait", "d"),
    (P, "Start-Process -FilePath docker -ArgumentList 'build','.'", "d"),
    (P, "saps npm -ArgumentList 'run','build' -NoNewWindow", "d"),
    (P, "Start-Process -FilePath 'dotnet.exe' -ArgumentList 'test Shop.sln' -Wait", "d"),
    (P, "& ('doc'+'ker') build .", "d"),
    (P, "$d='do'+'cker'; & $d build .", "d"),
    (B, "npm exec playwright test", "d"),
    (B, "npm exec -- playwright test", "d"),
    (B, "find . -name '*.csproj' -exec dotnet test {} \\;", "d"),
    (B, "find . -exec dotnet test {} +", "d"),
    (B, "find web -maxdepth 1 -execdir npm ci ';'", "d"),
    (B, "echo test | xargs dotnet", "d"),
    (B, "ls | xargs -n1 npm", "d"),
    (B, "cat imgs | xargs docker", "d"),
    (B, "ssh -o HostName=192.0.2.11 anyname docker ps", "b"),
    (B, "ssh -oHostName=ha x docker images", "b"),
    (B, "ssh -J haa 198.51.100.5 docker ps", "b"),
    (B, "ssh -o ProxyJump=deploy@192.0.2.11:22 box docker ps", "b"),
    (B, "ssh -o 'ProxyJump ha' box docker ps", "b"),
    (B, "ssh ha 'docker compose ps'", "b"),
    (B, "ssh ha -- docker ps", "b"),
    (B, "ssh ha -- rm -rf /x", "b"),
    (B, "ssh -- ha docker ps", "b"),
    (B, "ssh -p 22 -- deploy@192.0.2.11 -- docker ps", "b"),
    # rule 4 on any host
    (B, "docker compose down --rmi all", "c"),
    (B, "docker compose -f x.yml down --rmi=all", "c"),
    (B, "docker-compose down --rmi all", "c"),
    (B, "docker run -p 80:80 nginx", "c"),
    (B, "docker run -d --publish 8080:80 nginx", "c"),
    (B, "docker run --publish=8080:80 nginx", "c"),
    (B, "docker run -P nginx", "c"),
    (B, "docker run -dP nginx", "c"),
    (B, "docker run --publish-all nginx", "c"),
    (B, "docker create -p8080:80 nginx", "c"),
    (B, "docker container run -p 1:1 nginx", "c"),
    (B, "ssh hbx docker run -p 80:80 nginx", "c"),
    (B, "docker login -u x -p y", "c"),
    (B, "docker login --password=y reg", "c"),
    (B, "echo y | docker login -u x --password-stdin", "c"),
    (B, "systemctl restart docker", "c"),
    (B, "sudo systemctl stop docker.service", "c"),
    (B, "systemctl restart containerd", "c"),
    (B, "service docker restart", "c"),
    (B, "ssh hbx sudo service containerd stop", "c"),
    (P, "Restart-Service com.docker.service", "c"),
    (P, "Stop-Service -Name docker", "c"),
    (B, "echo '{}' > /etc/docker/daemon.json", "c"),
    (B, "echo '{}' | sudo tee /etc/docker/daemon.json", "c"),
    (B, "cp x.json /etc/docker/daemon.json", "c"),
    (P, "Set-Content C:\\ProgramData\\docker\\config\\daemon.json '{}'", "c"),
    (P, "Copy-Item x.json C:\\ProgramData\\docker\\config\\daemon.json", "c"),
    (B, "wsl --shutdown", "c"),
    (B, "wsl.exe --shutdown", "c"),
    (P, "wsl --shutdown", "c"),
    (B, "wsl --terminate Ubuntu", "c"),
    (P, "wsl.exe -t docker-desktop", "c"),
    # file copies touching the helper-only host (both directions)
    (B, "scp a.txt ha:/tmp", "b"),
    (B, "scp a deploy@192.0.2.11:/tmp", "b"),
    (B, "scp -P 22 haa:/x .", "b"),
    (B, "scp a scp://deploy@192.0.2.11/tmp", "b"),
    (B, "rsync -a . haa:/x", "b"),
    (B, "rsync -a . deploy@ha::mod", "b"),
    (B, "sftp ha", "b"),
    (B, "sftp -P 22 deploy@192.0.2.11", "b"),
    (P, "scp.exe a haa:/tmp", "b"),
    # lint is heavy
    (B, "npm run lint", "d"),
    (B, "pnpm run lint:ts", "d"),
    (B, "yarn run lint", "d"),
    # (p) production: destructive -> deny (pc prod-only; hb helper+prod, prod wins)
    (B, "ssh pc docker rm -f web", "p"),
    (B, "ssh pc 'docker compose down'", "p"),
    (B, "ssh hbx docker restart web", "p"),
    (B, "ssh pc 'docker volume rm shop_data'", "p"),
    (B, "ssh pc 'docker image prune -f --filter label=io.orch.agent=1'", "p"),
    (B, "ssh pc 'sudo rm -rf /srv/shop/releases/1'", "p"),
    (B, "ssh pc 'echo {} > /srv/shop/config.json'", "p"),
    (B, "ssh pc 'sed -i s/a/b/ /etc/nginx/nginx.conf'", "p"),
    (B, "ssh pc 'sudo chown -R www-data /srv'", "p"),
    (B, "ssh pc 'cd /srv/shop && git reset --hard origin/main'", "p"),
    (B, "ssh pc 'psql -c \"DROP TABLE orders\"'", "p"),
    (B, "ssh pc 'docker exec db psql -c \"DELETE FROM orders\"'", "p"),
    (B, "ssh pc 'pkill -f shop'", "p"),
    (B, "ssh pc sudo reboot", "p"),
    (B, "ssh pc 'truncate -s 0 /var/log/shop.log'", "p"),
    (B, "ssh pc 'find /srv/shop/tmp -delete'", "p"),
    (B, "ssh -p 2222 deploy@192.0.2.12 'docker stop web'", "p"),
    (B, "docker --context orch-hb rm -f web", "p"),
    (B, "docker -H ssh://deploy@192.0.2.20 stop web", "p"),
    (B, "DOCKER_HOST=ssh://deploy@192.0.2.20 docker compose down", "p"),
    (B, "ssh hbx /srv/shop/rollback.sh; ssh hbx 'rm -rf /srv/shop'", "p"),
    (P, "ssh pc 'Remove-Item C:/srv/shop -Recurse -Force'", "p"),
    (B, "ssh pc 'cat x | tee /etc/hosts'", "p"),
    (B, "ssh pc '> /var/log/shop.log'", "p"),
    (B, "ssh pc 'su -c \"rm -rf /srv/shop\"'", "p"),
    (P, "$env:DOCKER_HOST='ssh://hbx'; docker rm -f web", "p"),
    (B, "export DOCKER_CONTEXT=orch-hb; docker compose down", "p"),
    (B, "docker context use orch-hb && docker rm -f web", "a"),   # orch-* contexts are orch's (was p)
    (B, "export DOCKER_HOST=ssh://hbx; docker build .", "d"),
    (B, "cp -r evil/. C:/work/shop/.git/orch/", "s"),
    (B, "rsync -a x/ C:/work/shop/.git/orch/state/", "s"),
    (B, "mv new.json C:/work/shop/.git/orch", "s"),
    (B, "tar -xf x.tar -C C:/work/shop/.git/orch", "s"),
    # (s) orch setup files: the approved store and ORCH_HOME files are written only by orch
    (B, "echo '{}' > C:/work/shop/.git/orch/config.json", "s"),
    (B, "cp x.json C:/work/shop/.git/orch/config.json", "s"),
    (B, "mv C:/work/shop/.git/orch/config.json /tmp/x", "s"),
    (B, "echo '{}' >> .git/orch/state/runs.jsonl", "s"),
    (B, "tee -a C:/work/shop/.git/orch/state/runs.jsonl < x", "s"),
    (B, "sed -i s/a/b/ C:/work/shop/.git/orch/config.json", "s"),
    (B, "cd C:/work/shop/.git/orch && echo x > config.json", "s"),
    (B, "python -c \"open('C:/work/shop/.git/orch/config.json','w')\"", "s"),
    (B, "rm C:/work/shop/.git/orch/state/runs.jsonl", "s"),
    (B, "rm -rf C:/work/shop/.git", "s"),
    (P, "Set-Content -Path C:\\work\\shop\\.git\\orch\\config.json -Value '{}'", "s"),
    (P, "'{}' | Out-File C:/work/shop/.git/orch/state/hosts.json", "s"),
    (P, "Add-Content C:/work/shop/.git/orch/state/runs.jsonl x", "s"),
    (B, "echo '{}' >> C:/work/shop/.git/orch/journal/journal.jsonl", "s"),
    (B, "sed -i s/failed/done/ .git/orch/journal/MEMORY.md", "s"),
    (P, "Set-Content C:/work/shop/.git/orch/journal/MEMORY.md x", "s"),
    (P, "Remove-Item -Recurse C:\\work\\shop\\.git\\orch", "s"),
    (B, "echo '{}' > ~/.helper-orch/trust.json", "s"),
    (B, "cp t.json ~/.helper-orch/registry.json", "s"),
    (B, "rm ~/.helper-orch/guard-view.json", "s"),
    (B, "rm -rf ~/.helper-orch", "s"),
    (P, "Copy-Item v.json $HOME\\.helper-orch\\guard-view.json", "s"),
    # R20 ssh options placed AFTER the destination (OpenSSH getopt-permutes): still classified
    (B, "ssh haa -t docker system prune -af --volumes", "b"),
    (B, "ssh haa -q docker rm -f web", "b"),
    (B, "ssh deploy@192.0.2.11 -p 22 docker rm -f web", "b"),
    (B, "ssh anyname -o HostName=192.0.2.11 docker ps", "b"),
    (B, "ssh box -J haa docker ps", "b"),
    (B, "ssh box -o HostName=192.0.2.12 docker rm -f web", "p"),
    (B, "ssh pc -q rm -rf /srv/shop", "p"),
    (B, "ssh pc -T 'docker compose -f /srv/shop/compose.yml down'", "p"),
    (B, "ssh other-host -q docker volume prune -f", "c"),
    # R25 ssh -o RemoteCommand runs on the remote host; LocalCommand/ProxyCommand/KnownHostsCommand on the master
    (B, "ssh -o RemoteCommand='docker rm -f web' haa", "b"),
    (B, "ssh -oRemoteCommand=docker\\ rm\\ -f\\ web haa", "b"),
    (B, "ssh -o 'RemoteCommand=rm -rf /srv/shop' pc", "p"),
    (B, "ssh -o RemoteCommand='docker system prune -af' other-host", "c"),
    (B, "ssh -o PermitLocalCommand=yes -o LocalCommand='docker system prune -af' haa uptime", "c"),
    (B, "ssh -o ProxyCommand='docker system prune -af' box uptime", "c"),
    (B, "ssh -o KnownHostsCommand='docker volume prune -f' box uptime", "c"),
    # R21 Git Bash `cmd //c` (MSYS maps //c -> /c)
    (B, 'cmd //c "docker --context orch-ha ps"', "a"),
    (B, 'cmd //d //s //c "ssh pc rm -rf /srv/shop"', "p"),
    (B, "cmd.exe //c dotnet test", "d"),
    (B, 'cmd //c "echo 1 > ~/.helper-orch/ALLOW_LOCAL"', "e"),
    (B, 'cmd //C "docker system prune -af"', "c"),
    # R22 trailing dot/space on a path component (Windows drops it on write) still reaches the setup file
    (B, "echo '{}' > C:/work/shop/.git/orch/config.json.", "s"),
    (B, "echo '{}' > 'C:/work/shop/.git/orch/config.json. '", "s"),
    (B, "echo '{}' > C:/work/shop/.git/orch./config.json", "s"),
    (B, "echo '{}' >> C:/work/shop/.git/orch/state./runs.jsonl", "s"),
    (B, "python -c \"open('C:/work/shop/.git/orch/config.json.','w')\"", "s"),
    (P, "Set-Content -Path C:\\work\\shop\\.git\\orch\\config.json. -Value '{}'", "s"),
    (P, "Out-File -FilePath C:/work/shop/.git/orch/config.json.", "s"),
    (P, "Copy-Item v.json C:/work/shop/.git/orch/config.json.", "s"),
    # R23 wildcard/brace delete and find -delete/-exec covering a store or ORCH_HOME
    (B, "rm -f ~/.helper-orch/*", "s"),
    (B, "rm -f ~/.helper-orch/*.json", "s"),
    (B, "rm -f ~/.helper-orch/{guard-view,trust,registry}.json", "s"),
    (B, "find ~/.helper-orch -delete", "s"),
    (B, "find ~/.helper-orch -name guard-view.json -delete", "s"),
    (B, "find ~/.helper-orch -exec rm {} +", "s"),
    (B, "rm -f C:/work/shop/.git/orch/*", "s"),
    (B, "find C:/work/shop/.git/orch -delete", "s"),
    (P, "Remove-Item ~/.helper-orch/*", "s"),
    (P, "Remove-Item $HOME\\.helper-orch\\*.json", "s"),
    # R24 PowerShell braced ${env:DOCKER_HOST} / Set-Item env: assignment
    (P, "${env:DOCKER_HOST}='ssh://haa'; docker rm -f web", "a"),
    (P, "${env:DOCKER_HOST} = 'ssh://haa'; docker ps", "a"),
    (P, "${env:DOCKER_CONTEXT}='orch-ha'; docker ps", "a"),
    (P, "Set-Item -Path env:DOCKER_HOST -Value ssh://haa; docker ps", "a"),
    (P, "Set-Item env:DOCKER_HOST ssh://haa; docker rm -f web", "a"),
    (P, "${env:DOCKER_HOST}='ssh://hbx'; docker rm -f web", "p"),
    # R26 copy/move destination via -t/--target-directory/-Destination/robocopy/xcopy
    (B, "cp -t C:/work/shop/.git/orch /tmp/evil/config.json", "s"),
    (B, "cp --target-directory=C:/work/shop/.git/orch /tmp/evil/config.json", "s"),
    (B, "cp --target-directory C:/work/shop/.git/orch x.json", "s"),
    (B, "install -t C:/work/shop/.git/orch x.json", "s"),
    (B, "mv --target-directory=C:/work/shop/.git/orch /tmp/evil/config.json", "s"),
    (B, "cp -t ~/.helper-orch /tmp/evil/trust.json", "s"),
    (P, "Copy-Item -Destination C:\\work\\shop\\.git\\orch -Path C:\\tmp\\stage\\config.json", "s"),
    (P, "Copy-Item -Destination C:/work/shop/.git/orch/config.json -Path C:/tmp/stage/config.json", "s"),
    (P, "cpi -Destination $HOME\\.helper-orch -Path C:\\tmp\\stage\\trust.json", "s"),
    (B, "robocopy C:/tmp/stage C:/work/shop/.git/orch config.json", "s"),
    (B, "xcopy C:/tmp/stage/config.json C:/work/shop/.git/orch/ /Y", "s"),
    # R27 a profile write (not an orch heredoc) still caught
    (B, "cat >> ~/.bashrc <<'EOF'\nexport DOCKER_HOST=ssh://ha\nEOF", "a"),
    # R28 brace tokens no longer split an ssh command into an unseen remote tail
    (B, "ssh pc true {{x}} '; rm -rf /srv/shop'", "p"),
    (B, "ssh pc echo { '; rm -rf /srv/shop'", "p"),
    (B, "ssh ha true {{x}} '; docker ps'", "b"),
    # R40 only the user may set ORCH_HOME/ORCH_CONFIG and the test switches
    (B, "ORCH_HOME=/tmp/x " + ORCH + " run api-test", "e"),
    (B, "export ORCH_HOME=/tmp/x", "e"),
    (B, "ORCH_CONFIG=/tmp/legacy " + ORCH + " run api-test", "e"),
    (B, "export ORCH_CONFIG=/tmp/legacy && " + ORCH + " plan t-ok", "e"),
    (B, "ORCH_CI_CHILD='[\"ssh\",\"pc\"]' " + ORCH + " ci --jobs x", "e"),
    (B, "ORCH_FAULT=create@n1 " + ORCH + " run t-ok", "e"),
    (B, "ORCH_SELFTEST=1 " + ORCH + " run t-ok", "e"),
    (B, "ORCH_TEST_MODE=1 " + ORCH + " run t-ok", "e"),
    (B, "ORCH_GC_NODE_JSON='{}' " + ORCH + " gc --session-end --node h11", "e"),
    (B, "export ORCH_GC_NODE_JSON=x; " + ORCH + " gc --session-end", "e"),
    (P, "$env:ORCH_HOME='/tmp/x'; " + ORCH + " run api-test", "e"),
    (P, "${env:ORCH_CONFIG}='/tmp/x'; " + ORCH + " ci", "e"),
    (P, "setx ORCH_HOME C:/tmp/x", "e"),
    (P, "Set-Item -Path env:ORCH_HOME -Value /tmp/x", "e"),
    (P, "[Environment]::SetEnvironmentVariable('ORCH_HOME','/tmp/x','User')", "e"),
    # rule v: a command name held in a variable is replaced by every value the command gave it, then every rule
    (B, 'X="ssh hbx"; $X docker rm -f web', "p"),
    (B, "X='ssh ha'; $X docker ps", "b"),
    (B, "D=docker; $D system prune -af", "c"),
    (B, "D=docker; ${D} build .", "d"),
    (B, 'D="sudo docker"; $D build .', "d"),
    (B, "export D=/usr/bin/docker; $D --context ha ps", "a"),
    (B, 'C="docker --context orch-ha"; time $C ps', "a"),
    (B, "declare D=docker; nohup $D compose up -d &", "d"),
    (B, "local N=npm; sudo -E $N ci", "d"),
    (B, "typeset -x N=npm; env A=1 \"$N\" test", "d"),
    (B, "readonly R=rm; ssh pc \"$R -rf /srv/shop\"", "p"),
    (B, "T=touch; $T ~/.helper-orch/ALLOW_LOCAL", "e"),
    (B, "E=tee; echo '{}' | $E C:/work/shop/.git/orch/config.json", "s"),
    (B, "D=docker; [ -x /usr/bin/podman ] && D=echo; $D build .", "d"),   # every value counts, not the last one
    (B, "if true; then D=docker; fi; $D build .", "d"),
    (B, "for t in echo docker; do $t build .; done", "d"),
    (B, 'TOOLS="echo docker"; for t in $TOOLS; do "$t" build .; done', "d"),
    (B, "D=docker; X=\"$D build\"; $X .", "d"),
    (B, "D=$HOME/bin/docker; $D build .", "d"),         # an unknown $HOME only in the folder part: still docker
    (B, "D=docker; eval \"$D build .\"", "d"),
    (B, "D=docker; bash -c '$D system prune -f'", "c"),
    (B, "SH=bash; cat x.sh | ssh ha $SH", "b"),
    (B, "D=docker; find . -name Dockerfile -exec $D build {} \\;", "d"),
    (B, "D=docker; xargs -n1 $D build < dirs", "d"),
    (P, "$D = 'docker'; & $D system prune -af", "c"),
    (P, '$X = "ssh hbx"; & $X docker rm -f web', "p"),
    (P, "Set-Variable -Name D -Value docker; & $D build .", "d"),
    (P, "sv N npm; & $N ci", "d"),
    (P, "$env:D = 'docker'; & $env:D build .", "d"),
    (P, "$r = dotnet test", "d"),                       # PowerShell runs the command on an assignment's right side
    (P, "$out = ssh hbx docker rm -f web", "p"),
    (P, "& $tool test", "d"),                           # PowerShell & of an unresolved variable with build tokens
    (P, "& $O init --approve", "d"),
    # rule d: a tool C:/work/lab's own jobs use (its job index), whatever new_heavy says, launcher dropped
    (B, "cd C:/work/lab && python -m pytest -q", "d"),
    (B, "cd C:/work/lab && pytest -q tests", "d"),
    (B, "cd C:/work/lab && python3 -B -m pytest", "d"),
    (B, "cd C:/work/lab && .venv/bin/python -m pytest -x", "d"),
    (B, "cd C:/work/lab && py.test", "d"),
    (B, "cd C:/work/lab && uv run pytest", "d"),
    (B, "cd C:/work/lab && poetry run python -m pytest", "d"),
    (B, "cd C:/work/lab && python -m unittest discover -s tests", "d"),
    (B, "cd C:/work/lab && ruff check .", "d"),
    (B, "cd C:/work/lab && python -m ruff format --check", "d"),
    (B, "cd C:/work/lab && npx jest --ci", "d"),
    (B, "cd C:/work/lab && jest", "d"),
    (B, "cd C:/work/lab && pnpm exec jest", "d"),
    (B, "cd C:/work/lab && yarn dlx jest", "d"),
    (B, "cd C:/work/lab && npm run typecheck", "d"),
    (B, "cd C:/work/lab && pnpm run typecheck", "d"),
    (B, "cd C:/work/lab && yarn typecheck", "d"),
    (B, "cd C:/work/lab && bun run typecheck", "d"),
    (B, "cd C:/work/lab && npm --prefix web run typecheck -- --watch", "d"),
    (B, "pytest C:/work/lab/tests", "d"),
    (B, "env -C C:/work/lab python -m pytest", "d"),
    (B, "cd C:/work/lab && PY=python3 && $PY -m pytest", "d"),
    (P, "Set-Location C:/work/lab; python -m pytest -q", "d"),
    (P, "cd C:\\work\\lab; py -3 -m pytest", "d"),
    # Q20 blanking by raw span (never string-replace): an inert data word no longer hides a real assignment that
    # repeats its text later in the same command
    (B, "git commit --allow-empty -q -m docker_host; export DOCKER_HOST=ssh://ha; docker ps", "a"),
    (B, "git commit -q -m DOCKER_HOST; export DOCKER_HOST=tcp://192.0.2.20:2375; docker rm -f web", "p"),
    (B, "git commit -q -m orch_config; export ORCH_CONFIG=/tmp/legacy", "e"),
    (B, ORCH + " journal note docker_host; export DOCKER_HOST=ssh://ha; docker ps", "a"),
    (B, "gh pr create -b docker_context; export DOCKER_CONTEXT=orch-ha; docker ps", "a"),
    (P, "git commit -q -m docker_host; $env:DOCKER_HOST='ssh://deploy@192.0.2.11'; docker ps", "a"),
    (B, "git commit -q -m docker_host; cat >> ~/.bashrc <<'EOF'\nexport DOCKER_HOST=ssh://ha\nEOF", "a"),
    (B, 'git commit -q -m DOCKER_HOST""=ssh://ha; export DOCKER_HOST=ssh://ha; docker ps', "a"),
    # Q21 env scans catch the += append, ${VAR:=default} and pwsh ??= spellings (bash prefix, export, $env:/${env:})
    (B, "ORCH_HOME+=/x ORCH_CONFIG+=/tmp/legacy " + ORCH + " plan t-ok", "e"),
    (B, "ORCH_CI_CHILD+='[\"ssh\",\"pc\"]' ORCH_CONFIG+=/tmp/x " + ORCH + " ci --jobs x", "e"),
    (B, "export DOCKER_HOST+=ssh://ha; docker ps", "a"),
    (B, "export DOCKER_CONTEXT+=orch-ha; docker ps", "a"),
    (B, "${DOCKER_HOST:=ssh://ha}; docker ps", "a"),
    (B, "${DOCKER_CONTEXT:=orch-ha}; docker ps", "a"),
    (P, "$env:ORCH_HOME += '/tmp/x'; " + ORCH + " run api-test", "e"),
    (P, "${env:ORCH_CONFIG} += '/x'; " + ORCH + " ci", "e"),
    (P, "${env:ORCH_TEST_MODE}+='1'; " + ORCH + " run t-ok", "e"),
    (P, "$env:ORCH_HOME ??= '/tmp/x'; " + ORCH + " run t-ok", "e"),
    (P, "$env:DOCKER_HOST += 'ssh://deploy@192.0.2.20'; docker compose -p shop down", "p"),
    (P, "$env:DOCKER_HOST += 'ssh://haa'; docker rm -f web", "a"),
    # Q23 pass-through wrappers are unwrapped so the inner command meets every rule (including -c command strings)
    (B, "winpty ssh deploy@192.0.2.20 docker rm -f app", "p"),
    (B, "flock /tmp/l ssh pc docker rm -f app", "p"),
    (B, "flock /tmp/l -c 'ssh pc docker rm -f app'", "p"),
    (B, "strace -f docker --context ha ps", "a"),
    (B, "ltrace docker --context ha ps", "a"),
    (B, "strace -o /dev/null docker system prune -af", "c"),
    (B, "runuser -u deploy -- ssh pc rm -rf /srv/shop", "p"),
    (B, "runuser -u deploy -c 'docker system prune -af'", "c"),
    (B, "script -qc 'ssh pc rm -rf /srv/shop' /dev/null", "p"),
    (B, "sg docker -c 'docker volume prune -f'", "c"),
    (B, "systemd-run --scope --uid 1000 docker system prune -af", "c"),
    (B, "pkexec docker system prune -af", "c"),
    (B, "caffeinate docker system prune -af", "c"),
    (B, "parallel docker system prune -af ::: a", "c"),
    (B, "ssh haa flock /tmp/l docker rm -f x", "b"),
    (B, "ssh haa strace -f docker rm -f x", "b"),
    # rule v extended: a host/path held in a same-script variable is substituted in ssh/scp/docker/redirect targets
    (B, "H=hbx; ssh $H docker rm -f web", "p"),
    (B, "H=ha; ssh $H docker ps", "b"),
    (B, "H=192.0.2.20; ssh $H docker rm -f web", "p"),
    (B, "H=pc; ssh $H sudo reboot", "p"),
    (B, "H=ha; docker -H ssh://$H ps", "a"),
    (B, "D=orch-ha; docker --context $D ps", "a"),
    (B, "D=~/.helper-orch; echo x > $D/trust.json", "s"),
    (B, "T=~/.helper-orch/trust.json; rm -f $T", "s"),
    (P, "$H='hbx'; docker -H ssh://$H rm -f web", "p"),
    # MF2 ssh whose remote command holds a substitution the master's shell runs first ($(...) or backticks outside
    # single quotes, an unquoted-delimiter heredoc, PowerShell $(...), (...), @(...), {...}, @"..."@): the remote
    # command is unknown until it runs, so a helper-only host denies it; destructive text on production still denies
    (B, "ssh ha \"ls; echo $(docker ps)\"", "b"),          # was allowed: the output of a local docker ps ran there
    (B, "ssh ha \"$(cat cmd.txt)\"", "b"),
    (B, "ssh ha \"uptime; `cat cmd.txt`\"", "b"),
    (B, "ssh haa uptime $(cat args.txt)", "b"),
    (B, "ssh deploy@192.0.2.11 \"echo $(date)\"", "b"),
    (B, "ssh -o RemoteCommand=\"$(cat c)\" ha", "b"),
    (B, "ssh ha <<EOF\nuptime $(cat x)\nEOF", "b"),
    (B, "ssh ha bash <<EOF\n`cat cleanup.sh`\nEOF", "b"),
    (B, "echo \"$(cat c)\" | ssh ha bash", "b"),
    (B, "cat x.sh | ssh ha 'bash -s'", "b"),              # a quoted remote shell still reads the stdin script
    (B, "x=$(ssh ha \"ls $(pwd)\")", "b"),
    (B, "H=ha; ssh $H \"$(cat c)\"", "b"),
    (P, "ssh ha \"$(Get-Content cmd.txt)\"", "b"),
    (P, "ssh ha $(Get-Content cmd.txt)", "b"),
    (P, "ssh ha (Get-Content cmd.txt)", "b"),
    (P, "ssh ha (& { 'docker ps' })", "b"),
    (P, "ssh haa @('uptime', (Get-Content x))", "b"),
    (P, "ssh ha {docker rm -f x}", "b"),
    (P, "ssh ha @\"\n$(Get-Content x)\n\"@", "b"),
    (B, "ssh pc \"docker rm -f $(cat id)\"", "p"),
    # an unquoted heredoc's $(...) runs on the master before the command reads it; a \EOF delimiter is quoted
    (B, "cat <<EOF\n$(docker system prune -af)\nEOF", "c"),
    (B, "cat <<\\EOF\nx\nEOF\ndocker system prune -f", "c"),
    # orch creates, updates and removes its own orch-* docker contexts
    (B, "docker context use orch-hb", "a"),                 # was ask (p)
    (P, "docker context use ORCH-HB", "a"),
    (B, "docker context use orch-x", "a"),
    (B, "docker context update orch-ha --docker host=ssh://deploy@198.51.100.9", "a"),
    (B, "docker context rm orch-hb", "a"),
    (B, "docker context rm -f old orch-pc-1a2b3c4d", "a"),
    (B, "docker context remove orch-ha", "a"),
    (B, "docker context create orch-zz --docker host=ssh://deploy@198.51.100.9", "a"),
]

# (tool, command, rule) -> ask
ASK = [
    # (p) production: any other change asks
    (B, "ssh pc 'docker compose up -d'", "p"),
    (B, "ssh deploy@192.0.2.20 docker pull shop:2", "p"),
    (B, "ssh hbx 'cd /srv/shop && docker compose up -d'", "p"),
    (B, "ssh -p 2222 deploy@192.0.2.12 dotnet test", "p"),
    (B, "ssh pc 'sudo systemctl restart nginx'", "p"),
    (B, "ssh pc 'echo ok >> /srv/shop/notes.txt'", "p"),
    (B, "ssh pc 'rm /tmp/x.txt'", "p"),
    (B, "ssh pc 'psql -c \"select 1\"'", "p"),
    (B, "ssh pc 'docker exec web sh'", "p"),
    (B, "ssh pc 'curl -X POST http://127.0.0.1:8080/reindex'", "p"),
    (B, "ssh pc 'curl -o /tmp/x.tar http://127.0.0.1/x.tar'", "p"),
    (B, "ssh pc 'wget http://127.0.0.1/x.tar'", "p"),
    (B, "ssh pc 'find /srv -name x -exec cat {} +'", "p"),
    (B, "ssh pc 'cat x | tee -a /srv/shop/notes.txt'", "p"),
    (B, "git push deploy@192.0.2.20:/srv/shop.git main", "p"),
    (B, "git push ssh://deploy@192.0.2.12:2222/srv/shop.git", "p"),
    (B, "scp pc:/srv/a.txt hbx:/tmp/", "p"),
    (B, "ssh hbx /srv/shop/other.sh", "p"),
    (P, "ssh pc Restart-Service W3SVC", "p"),
    (B, "scp a.tar hbx:/tmp/", "p"),
    (B, "scp -P 2222 dist.tar deploy@192.0.2.12:/srv/shop", "p"),
    (B, "rsync -a dist/ pc:/srv/shop/", "p"),
    (B, "rsync -a pc:/var/log/ logs/", "p"),
    (B, "sftp deploy@192.0.2.20", "p"),
    (B, "git archive HEAD | ssh hbx tar x -C /srv/shop", "p"),
    (B, "cat deploy.sh | ssh pc", "p"),
    (B, "ssh pc bash -s < migrate.sh", "p"),
    (B, "ssh pc", "p"),
    (B, "docker --context orch-hb compose up -d", "p"),
    (B, "docker -H ssh://deploy@192.0.2.20 run -d shop:2", "p"),
    (B, "DOCKER_HOST=ssh://deploy@192.0.2.20 docker compose pull", "p"),
    (B, "export DOCKER_HOST=ssh://hbx && docker ps", "p"),
    (P, "$env:DOCKER_CONTEXT='orch-hb'; docker ps", "p"),
    (B, "docker context create p1 --docker host=ssh://deploy@192.0.2.20", "p"),
    # a registered deploy/rollback command does not cover what is chained after it
    (B, "ssh hbx /srv/shop/rollback.sh && scp x.txt hbx:/tmp", "p"),
    (B, "./scripts/deploy.sh && ssh pc touch /srv/shop/flag", "p"),
    (B, "ssh hbx /srv/shop/rollback.sh '&& touch /srv/x'", "p"),
    (B, "ssh hbx /srv/shop/rollback.sh --force", "p"),
    # (i) saving the setup is the user's call, by any invocation path
    (B, ORCH + " init --approve", "i"),
    (B, "bash ~/.claude/skills/helper-node-orchestrator/scripts/orch init --approve", "i"),
    (B, "~/.claude/skills/helper-node-orchestrator/scripts/orch init --offline --approve", "i"),
    (B, "python -B scripts/orch.py init --approve", "i"),
    (B, "ORCH_PYTHON=python3 bash scripts/orch init --project C:/work/shop --approve", "i"),
    (P, "bash \"$HOME/.claude/skills/helper-node-orchestrator/scripts/orch\" init --approve", "i"),
    (B, "cd C:/work/shop && bash scripts/orch init --appro", "i"),
    # R39 the approval entry point cannot be reached around the prompt by any invocation path
    (B, "python scripts/orch_init.py --approve", "i"),
    (B, "python -B scripts/orch_init.py --approve --offline", "i"),
    (B, "python -m orch init --approve", "i"),
    (P, "python -m orch init --approve", "i"),
    (B, "cd scripts && python -m orch init --approve", "i"),
    (B, "PYTHONPATH=scripts python -m orch init --approve", "i"),
    (B, "py scripts/orch_init.py --project C:/work/shop --approve", "i"),
    (B, "python -m orch init --appr", "i"),
    (B, "python scripts/orch_init.py --project C:/work/shop --appr", "i"),
    # rule v: a variable command name is resolved (rule i applies) ...
    (B, 'O="bash C:/work/skills/helper-node-orchestrator/scripts/orch"; $O init --approve | tail -1', "i"),
    (B, "O=orch; ${O} init --approve", "i"),
    (B, "O='bash ~/.claude/skills/helper-node-orchestrator/scripts/orch'\n$O init --offline --approve", "i"),
    (B, 'export O="python -m orch"; $O init --approve', "i"),
    (P, '$O = "bash C:/work/skills/helper-node-orchestrator/scripts/orch"; & $O init --approve', "i"),
    (P, "Set-Variable -Name O -Value 'python -m orch'; & $O init --approve", "i"),
    # ... and one the guard cannot resolve asks when the rest looks relevant
    (B, "$O init --approve", "v"),
    (B, "O=$(command -v orch); $O init --approve", "v"),
    (B, "O=(bash scripts/orch); \"${O[@]}\" init --approve", "v"),
    (B, "read -r O < .orch-cmd; $O init --approve", "v"),
    (B, "$O init --approve; O=orch", "v"),
    (B, "${O:-orch} init --appr", "v"),
    (B, "\"$(command -v orch)\" init --approve", "v"),
    (B, "$X hbx docker rm -f web", "v"),
    (B, "$SSH deploy@192.0.2.20 docker compose down", "v"),
    (B, "$CP dist.tar hbx:/srv/shop/", "v"),
    (B, "$D -H ssh://deploy@192.0.2.11 ps", "v"),
    (B, "$D system prune -af", "v"),
    (B, "$D build -t x .", "v"),
    (B, "$PY -m pytest -q", "v"),
    (B, "\"$(command -v dotnet)\" test", "v"),
    (B, "$NPM run lint", "v"),
    (B, "$SHELL -c 'npm ci'", "v"),
    (B, "D=(docker); $D build .", "v"),
    (B, "D=echo; read D < f; $D build .", "v"),
    (B, "O=echo; source ./env.sh; $O docker build .", "v"),
    (B, "cat x.sh | ssh ha $SH", "v"),
    (B, "ssh ha '$X build .'", "v"),
    # Q22 the approval entry point is reached by attached -m, -m runpy and inline runpy too (all must ask)
    (B, "python -morch init --approve", "i"),
    (B, "python -morch_init --approve", "i"),
    (B, "python -Bmorch init --approve", "i"),
    (B, "python -X utf8 -morch init --approve", "i"),
    (B, "python --check-hash-based-pycs never -m orch init --approve", "i"),
    (B, "python -m runpy orch init --approve", "i"),
    (B, "python -m runpy orch_init --appr", "i"),
    (B, "python -c \"import runpy; runpy.run_path('scripts/orch_init.py'); approve\"", "i"),
    (P, "python -Imorch init --approve", "i"),
    # rule v extended: an unresolvable host toward a mutating command, or an unknown wrapper cloaking a sensitive
    # command, asks the user to spell it out
    (B, "H=$(cat f); ssh $H docker rm -f web", "v"),
    (B, "P=$(cat f); scp a.tar $P:/srv", "v"),
    (B, "myprog ssh pc docker rm -f web", "v"),
    (B, "myprog ssh ha docker ps", "v"),
    (B, "unknownwrap " + ORCH + " init --approve", "v"),
    # a resolved variable host / upload to production still asks via rule p
    (B, "P=hbx; scp dist.tar $P:/srv/shop", "p"),
    (B, "P=hbx; rsync -a dist/ $P:/srv/shop/", "p"),
    (B, "winpty ssh pc docker compose up -d", "p"),
    # MF2 production (also helper + production): a substitution the master runs first in ssh's remote command asks
    (B, "ssh pc \"$(cat cmd.txt)\"", "p"),
    (B, "ssh pc \"echo hi; $(cat cmd.txt)\"", "p"),
    (B, "ssh pc \"`cat cmd.txt`\"", "p"),
    (B, "ssh pc docker $(echo rm) -f web", "p"),
    (B, "ssh hbx \"$(cat cmd.txt)\"", "p"),
    (B, "ssh -p 2222 deploy@192.0.2.12 docker logs web --since \"$(date +%s)\"", "p"),
    (B, "ssh -o RemoteCommand=\"$(cat c)\" pc", "p"),
    (B, "ssh pc <<EOF\n$(cat cmd.txt)\nEOF", "p"),
    (B, "ssh pc 'bash -s' <<EOF\necho `hostname`\nEOF", "p"),
    (B, "printf '%s\\n' \"$(cat c)\" | ssh pc", "p"),
    (P, "ssh pc \"$(Get-Content cmd.txt)\"", "p"),
    (P, "ssh pc $(& {'docker rm -f web'})", "p"),
    (P, "ssh hbx (Get-Content cmd.txt)", "p"),
    # ... and a remote command name the guard cannot tell, on a helper too (rule v: the command names the host)
    (B, "C=$(cat cmd.txt); ssh ha \"$C\"", "v"),
    (B, "ssh ha '$(cat /tmp/cmd)'", "v"),
    (B, "ssh ha 'uptime; $NEXT'", "v"),
    (P, "$c = Get-Content x; ssh ha $c", "v"),
    # MF4 inline code the guard cannot read asks when it names a helper or production host (any view identifier)
    # or a protected path (ORCH_HOME, its setup files or variable, a store, ALLOW_LOCAL)
    (B, "python -c \"import os; os.system('ssh pc docker rm -f web')\"", "x"),
    (B, "python3 -c \"import subprocess; subprocess.run(['ssh', 'hbx', 'docker', 'rm', '-f', 'web'])\"", "x"),
    (B, "py -3 -c \"import socket; socket.create_connection(('192.0.2.20', 22))\"", "x"),
    (B, "python - <<'EOF'\nimport os\nos.remove(os.path.expanduser('~/.helper-orch/guard-view.json'))\nEOF", "x"),
    (B, "echo \"import os; os.system('ssh pc reboot')\" | python3", "x"),
    (B, "python -c \"import os; print(os.environ['ORCH_HOME'])\"", "x"),
    (B, "cd C:/work/shop && python -c \"import shutil; shutil.rmtree('.git/orch/state')\"", "x"),
    (B, "node -e \"require('child_process').execSync('ssh ha docker ps')\"", "x"),
    (B, "node -p \"require('child_process').execSync('docker --context orch-ha ps').toString()\"", "x"),
    (B, "node --eval=\"require('fs').rmSync(require('os').homedir() + '/.helper-orch/guard-view.json')\"", "x"),
    (B, "bun -e \"Bun.spawnSync(['ssh', 'deploy@192.0.2.20', 'reboot'])\"", "x"),
    (B, "deno eval \"new Deno.Command('ssh', {args: ['pc', 'reboot']}).spawnSync()\"", "x"),
    (B, "ruby -e 'system(\"ssh pc reboot\")'", "x"),
    (B, "perl -e 'system(\"ssh pc reboot\")'", "x"),
    (B, "perl -le 'unlink \"$ENV{HOME}/.helper-orch/guard-view.json\"'", "x"),
    (B, "php -r 'system(\"ssh pc reboot\");'", "x"),
    (B, "osascript -e 'do shell script \"ssh pc reboot\"'", "x"),
    (B, "pwsh -c \"[Diagnostics.Process]::Start('ssh','pc docker rm -f web')\"", "x"),
    (P, "powershell -NoProfile -Command \"Invoke-Command -ComputerName pc -ScriptBlock { Restart-Service W3SVC }\"",
     "x"),
    (B, "ssh ha 'python3 -c \"import os; os.system(\\\"ssh pc reboot\\\")\"'", "x"),
]

# (tool, command) -> no decision
ALLOW = [
    (B, "git commit -m \"fix docker build docs\""),
    (B, "grep -r \"dotnet test\" ."),
    (B, "cat run-tests.sh"),
    (B, "ls ~/.helper-orch"),
    (B, "echo docker"),
    (B, ORCH + " run api-test"),
    (B, ORCH + " nodes"),
    (B, ORCH + " run shell-investigate --node ha -- docker ps"),
    (B, ORCH + " run web-test --node ha -- npm test"),
    (B, ORCH + " gc --session-end"),
    (B, ORCH + " init"),
    (B, ORCH + " init --show"),
    (B, ORCH + " init --apply <<'EOF'\n{\"helper_count\": 2}\nEOF"),
    (B, "~/.claude/skills/helper-node-orchestrator/scripts/orch run api-build --ref HEAD"),
    (B, "bash remote-tests.sh full"),
    (B, "bash checkpoint.sh create && bash checkpoint.sh verify"),
    (B, "./deploy-both.sh"),
    (B, "bash push.sh"),
    (B, "NODE=ha bash build.sh"),
    (B, "ssh ha uptime"),
    (B, "ssh ha 'uptime; free -m; df -h'"),
    (B, "ssh haa cat /proc/meminfo"),
    (B, "ssh -p 22 deploy@192.0.2.11 'ls -la /opt; ps aux | head'"),
    (B, "ssh ha journalctl --no-pager -n 100 -u docker"),
    (B, "docker ps"),
    (B, "docker images"),
    (B, "docker logs --tail 50 shop-web"),
    (B, "docker --context default ps"),
    (B, "docker image prune -f --filter label=io.orch.agent=1"),
    (B, "docker buildx prune --builder agentx-builder -f"),
    (B, "docker container prune -f --filter label=io.orch.agent=1"),
    (B, "docker run --rm alpine:3.20 echo hi"),
    (B, "docker run --rm alpine node"),
    (B, "dotnet --info"),
    (B, "npm --version"),
    (B, "yarn --version"),
    (B, "cat ~/.helper-orch/ALLOW_LOCAL"),
    (B, "test -f ~/.helper-orch/ALLOW_LOCAL && echo on"),
    (B, "rm -f ~/.helper-orch/ALLOW_LOCAL"),
    (P, "Test-Path ~/.helper-orch/ALLOW_LOCAL"),
    (B, "echo $HELPER_ORCH_LOCAL"),
    (B, "git commit -m \"$(cat <<'EOF'\nfix docker build docs; don't run dotnet test locally\n\nCo-Authored-By: x\nEOF\n)\""),
    (B, "git log --grep 'docker system prune'"),
    (B, "echo 'docker system prune is denied by the guard'"),
    (B, "grep -n 'ALLOW_LOCAL' guard.py"),
    (B, "cat > notes.md <<'EOF'\ndocker build . is heavy\ndotnet test too\nEOF"),
    (B, "ssh 198.51.100.52 docker ps"),
    (P, "Get-Content build.ps1 | Select-String 'docker build'"),
    (P, "Write-Output 'npm test'"),
    (P, "docker ps --format '{{.Names}}'"),
    (B, "wsl -l -v"),
    (B, "python guard.py < sample.json"),
    (B, "git diff -- hooks/test_guard.py"),
    (B, "git commit -m \"fix; docker build docs\""),
    (B, "gh pr create --body \"run docker system prune\""),
    (P, "docker ps; if ($LASTEXITCODE -ne 0) { exit 1 }"),
    (B, "docker compose ps"),
    (B, "docker compose logs up"),
    (B, "mkdir -p ~/.helper-orch/results"),
    (B, "cat ~/.helper-orch/audit.jsonl"),
    (B, "ls ~/.helper-orch/*"),
    (P, "Get-ChildItem C:\\Users\\dev\\.helper-orch"),
    (B, "dotnet build --help"),
    (B, "dotnet test --list-tests"),
    (B, "dotnet -?"),
    (B, "npm test -- --help"),
    (B, "pnpm install --version"),
    (B, "yarn -h"),
    (B, "bash C:/work/shop/deploy/deploy-both.sh"),
    (B, "bash C:/work/shop/deploy/checkpoint.sh create"),
    (B, "ssh 203.0.113.210 'systemctl status x'"),
    (B, "ssh -o HostName=203.0.113.210 x uptime"),
    (B, "ssh -J haa 198.51.100.5 uptime"),
    (B, "bash ~/.claude/skills/redeploy-app/scripts/remote.sh deploy"),
    (B, "BASE_URL=http://192.0.2.12:8452 python scripts/api_probe.py examples/cases.json"),
    (B, "find . -name '*.cs' -exec grep -l docker {} +"),
    (B, "echo a | xargs grep foo"),
    (B, "ls | xargs dotnet --version"),
    (B, "npm exec eslint ."),
    (B, "python -m pip list"),
    (B, "pip show requests"),
    (B, "go vet ./..."),
    (B, "cargo fmt"),
    (B, "cat ~/.helper-orch/guard.json"),
    (P, "Start-Process notepad -ArgumentList x.txt"),
    (P, "Start-Process dotnet -ArgumentList '--info'"),
    (P, "& $exe --version"),
    (B, "ssh ha -- uptime"),
    (B, "docker compose down"),
    (B, "docker compose down --rmi local"),
    (B, "docker run --rm -e P=1 alpine echo -p"),
    (B, "docker login -u x reg"),
    (B, "systemctl status docker"),
    (B, "systemctl restart nginx"),
    (P, "Get-Service *docker*"),
    (B, "cat /etc/docker/daemon.json"),
    (B, "cp /etc/docker/daemon.json backup.json"),
    (B, "wsl -e uname -a"),
    (B, "scp a.txt b.txt"),
    (B, "rsync -a src/ dst/"),
    (B, "sftp files.example.com"),
    (B, "npm run lintfix-docs"),
    # production: read-only commands pass (prod wins on hb, so read-only docker over ssh is fine there)
    (B, "ssh hbx 'docker compose ps'"),
    (B, "ssh -p 2222 deploy@192.0.2.12 'docker compose ps'"),
    (B, "ssh pc docker ps"),
    (B, "ssh pc 'docker logs --tail 100 web 2>&1 | grep -i error'"),
    (B, "ssh deploy@192.0.2.20 'docker compose ps; docker compose logs --tail 50'"),
    (B, "ssh hbx 'df -h; free -m; uptime; systemctl status nginx'"),
    (B, "ssh pc 'curl -fsS http://127.0.0.1:8080/health'"),
    (B, "ssh pc 'curl -sI https://127.0.0.1/ -o /dev/null'"),
    (B, "ssh pc 'wget -qO- http://127.0.0.1:8080/health'"),
    (B, "ssh pc journalctl -u shop --since today --no-pager"),
    (B, "ssh pc 'top -bn1 | head -20'"),
    (B, "ssh pc 'ls -la /srv/shop; cat /srv/shop/VERSION 2>/dev/null'"),
    (B, "ssh pc 'find /var/log -name \"*.log\" -mtime -1'"),
    (B, "ssh pc docker inspect web"),
    (B, "ssh pc 'docker exec web cat /etc/hosts'"),
    (B, "docker --context orch-hb ps"),
    (B, "docker -H ssh://deploy@192.0.2.20 logs web"),
    (B, "DOCKER_HOST=ssh://deploy@192.0.2.20 docker ps"),
    (B, "scp pc:/var/log/shop.log ."),
    (B, "scp -P 2222 hbx:/srv/shop/app.log logs/"),
    (B, "echo 'docker ps' | ssh pc"),
    (B, "ssh pc <<'EOF'\nuptime\ndocker ps\nEOF"),
    (B, "ssh -G hbx"),
    (B, "ssh -N -L 8080:127.0.0.1:80 pc"),
    (B, "ssh pc 'su -c \"docker ps\"'"),
    # registered deploy/rollback commands of the approved project
    (B, "./scripts/deploy.sh"),
    (B, "cd C:/work/shop && ./scripts/deploy.sh"),
    (B, "ssh hbx /srv/shop/rollback.sh"),
    # reading orch setup files is fine; another folder's .git/orch is not an approved store
    (B, "cat C:/work/shop/.git/orch/config.json"),
    (B, "cp C:/work/shop/.git/orch/config.json /tmp/config.bak"),
    (B, "ls -la C:/work/shop/.git/orch/state"),
    (B, "cd C:/work/shop/.git/orch && cat config.json"),
    (B, "grep -c pass .git/orch/state/runs.jsonl"),
    (B, "tail -n 20 C:/work/shop/.git/orch/journal/journal.jsonl"),
    (B, "cat ~/.helper-orch/trust.json"),
    (P, "Get-Content C:\\work\\shop\\.git\\orch\\config.json"),
    (B, "git commit -m \"docs: never edit ~/.helper-orch/trust.json\""),
    (B, "echo x > C:/work/other/.git/orch/config.json"),
    (B, "cp README.md C:/work/shop/docs/"),
    (B, "mkdir -p ~/.helper-orch"),
    (B, "du -sh ~/.helper-orch"),
    (B, "find C:/work/shop/.git/orch -name '*.json'"),
    (B, "cp -r C:/work/shop/.git/orch /tmp/orch-backup"),
    (B, "git push origin main"),
    (P, "ssh pc 'Get-Process | Select-Object -First 5'"),
    # R20 near-miss: options after the host on a read-only/tunnel command stay allowed
    (B, "ssh pc -N -L 8080:127.0.0.1:80"),
    (B, "ssh haa -4 uptime"),
    # R25 a read-only RemoteCommand is allowed (was wrongly asked as an interactive session)
    (B, "ssh -o RemoteCommand=uptime pc"),
    (B, "ssh -o RemoteCommand='docker ps' pc"),
    # R21 near-miss: read-only cmd //c
    (B, 'cmd //c "docker ps"'),
    (B, "cmd //c echo hi"),
    # R22 near-miss: a different extension past the trailing dot is not a setup file
    (P, "Set-Content C:/work/shop/.git/orch/config.json.bak x"),
    # R23 near-miss: listing/globbing without a deleter, and a look-alike sibling directory
    (B, "find ~/.helper-orch -name '*.json'"),
    (B, "rm -f ~/.helper-orch-notes/*"),
    # R24 near-miss: unknown host, or merely reading the variable
    (P, "${env:DOCKER_HOST}='ssh://198.51.100.7'; docker ps"),
    (P, "echo ${env:DOCKER_HOST}"),
    # R26 near-miss: copying OUT of the store, or into a non-setup folder
    (B, "cp -t /tmp/backup C:/work/shop/.git/orch/config.json"),
    (B, "cp -t C:/work/shop/docs README.md"),
    # R27 orch's own journal stdin and a git/gh message are inert data, even when they name a known host
    (B, ORCH + " journal add <<'EOF'\n{\"type\":\"difficulty\",\"detail\":\"past DOCKER_HOST=ssh://deploy@192.0.2.11 incident\"}\nEOF"),
    (B, ORCH + " journal end <<'EOF'\n{\"lessons\":[\"never export DOCKER_HOST=tcp://192.0.2.20:2376\"]}\nEOF"),
    (B, "git commit -m 'docs: explain ALLOW_LOCAL and DOCKER_HOST=ssh://haa'"),
    (B, "gh pr create --body 'set DOCKER_CONTEXT=orch-ha then run the job'"),
    # R28 go-template read-only prod docker checks no longer needlessly ask
    (B, "ssh pc docker inspect -f '{{.State.Health.Status}}' web"),
    (B, "ssh pc 'docker ps --format {{.Names}}'"),
    (B, "ssh pc docker ps --format '{{.Names}} {{.Status}}'"),
    # R39 near-miss: a non-approval orch entry point still passes unanalysed
    (B, "python scripts/orch_init.py --show"),
    (B, "python -m orch run api-test"),
    # R40 near-miss: reading the variable, or a non-user-only ORCH_ variable, is fine
    (B, "echo $ORCH_HOME"),
    (B, "ORCH_PYTHON=python3 bash scripts/orch run api-test"),
    # rule v near-misses: the orch journal pattern, a variable used as an argument or inside quoted text
    (B, 'O="bash C:/work/skills/helper-node-orchestrator/scripts/orch"; $O journal start <<\'EOF\'\n'
        '{"summary": "fix the docker build; DOCKER_HOST=ssh://ha was stale", "request": "ssh hbx docker rm"}\n'
        'EOF\n$O init'),
    (B, "O='bash ~/.claude/skills/helper-node-orchestrator/scripts/orch'; $O journal add <<'EOF'\n"
        "{\"type\":\"note\",\"detail\":\"HELPER_ORCH_LOCAL=1 was never set\"}\nEOF"),
    (B, 'O="bash C:/work/skills/helper-node-orchestrator/scripts/orch"; $O run api-test && $O ci --jobs web-test'),
    (B, "O=orch; $O init --show"),
    (B, "O=orch; echo $O init --approve"),
    (B, 'echo "$O init --approve"'),
    (B, "printf '%s\\n' \"$O init --approve\""),
    (B, "D=docker; echo $D build ."),
    (B, "D=docker; $D ps"),
    (B, "D=docker; ls $D"),
    (B, "X=ls; $X -la ~/.helper-orch"),
    (B, "for f in a b; do echo $f; done"),
    (B, "$EDITOR notes.md"),
    (B, "$PAGER build.log"),
    (B, "\"$(command -v python3)\" --version"),
    (B, "G=git; $G commit -m 'docs: DOCKER_HOST=ssh://haa and ALLOW_LOCAL'"),
    (P, '$O = "bash C:/work/skills/helper-node-orchestrator/scripts/orch"; & $O init'),
    (P, "$out = docker ps"),
    (P, "$n = 5; $n"),
    (P, "$msg = 'docker build failed'"),
    # rule d near-misses: tools C:/work/shop's jobs do not use (warn only), light flags, other tools in C:/work/lab
    (B, "python -m pytest -q"),
    (B, "ruff check ."),
    (B, "npm run typecheck"),
    (B, "cd C:/work/lab && pytest --version"),
    (B, "cd C:/work/lab && python -m pip list"),
    (B, "cd C:/work/lab && python scripts/gen.py"),
    (B, "cd C:/work/lab && npm run format"),
    (B, "cd C:/work/lab && bun test"),
    (B, "cd C:/work/lab && mypy ."),
    (B, "cd C:/work/lab && git log --grep pytest"),
    # Q20 near-miss: inert data that only mentions a token is not scanned, and blanking it does not touch the rest
    (B, ORCH + " journal add <<'EOF'\n{\"d\":\"old ORCH_HOME=/tmp and DOCKER_HOST=ssh://ha\"}\nEOF"),
    (B, "git commit -m 'docs: mention ORCH_HOME and ORCH_CONFIG'"),
    (B, "git commit -q -m docker_host && echo done"),
    # Q21 near-miss: an append on a similarly-named variable is not a user-only var
    (B, "ORCH_HOMEX+=/tmp/x bash scripts/orch run api-test"),
    # Q22 near-miss: an inline runpy orch run without an approval token passes unanalysed
    (B, "python -c 'import runpy; runpy.run_module(\"orch\")'"),
    (B, "python -morch run api-test"),
    (B, "python -m runpy orch run api-test"),
    # Q23 near-miss: a wrapper around a read-only or benign command stays allowed; an unknown prog with a
    # harmless tail is not asked
    (B, "winpty ssh ha uptime"),
    (B, "winpty docker ps"),
    (B, "flock /tmp/l ls"),
    (B, "strace -f git status"),
    (B, "myprog cat /etc/hosts"),
    (B, "myprog --flag build ."),
    # rule v extended near-miss: a read-only ssh with a variable host, and echoing a host variable
    (B, "H=ha; ssh $H uptime"),
    (B, "H=$(cat f); ssh $H uptime"),
    (B, "echo $H"),
    (B, "H=hbx; echo deploying to $H"),
    # MF2 near-miss: a substitution in single quotes, escaped, or in a quoted heredoc runs on the remote host and
    # meets the read-only rules there; ssh inside a local substitution, an unknown host, a local heredoc's $(...)
    (B, "ssh pc 'docker ps --format \"{{.Names}} $(date)\"'"),
    (B, "ssh pc 'docker logs --since $(date -d \"1 hour ago\" +%s) web'"),
    (B, "ssh ha 'echo $(hostname)'"),
    (B, "ssh pc \"echo \\$(date)\""),
    (B, "ssh pc <<'EOF'\necho $(date)\nEOF"),
    (B, "up=$(ssh ha uptime)"),
    (B, "ssh 198.51.100.52 \"$(cat cmd.txt)\""),
    (B, "cat <<EOF > notes.md\nbuilt on $(date)\nEOF"),
    (B, "cat <<\\EOF\n$(docker system prune -af)\nEOF"),
    (P, "if ($ok) { ssh ha uptime }"),
    (P, "ssh ha uptime; (Get-Date)"),
    # MF4 near-miss: inline code naming no host and no protected path, and a script's own -e argument
    (B, "python -c \"print(1)\""),
    (B, "python3 -c \"import json,sys; print(json.load(sys.stdin)['x'])\" < out.json"),
    (B, "python - <<'EOF'\nprint('hello')\nEOF"),
    (B, "node -e \"console.log(process.version)\""),
    (B, "node app.js -e 'ssh pc'"),
    (B, "perl -pe 's/foo/bar/' notes.txt"),
    (B, "ruby -e 'puts 1'"),
    (B, "deno eval \"console.log(1)\""),
    (P, "pwsh -c \"Get-ChildItem C:/work/shop\""),
    # orch's own contexts can be read; other contexts keep the rules they had
    (B, "docker context inspect orch-ha"),
    (B, "docker context ls"),
    (B, "docker context use default"),
    (B, "docker context create dev --description orch-like --docker host=unix:///var/run/docker.sock"),
]

DC = "mcp__plugin_desktop-commander_desktop-commander__"
# MF3 (tool, tool_input, decision, rule): MCP calls; decision None = no decision. Desktop Commander's shapes.
MCP = [
    # a command tool's command meets every shell rule (both dialects unless an input names the shell)
    (DC + "start_process", {"command": "docker --context ha ps", "timeout_ms": 30000}, "deny", "a"),
    (DC + "start_process", {"command": "ssh pc docker rm -f web", "timeout_ms": 30000}, "deny", "p"),
    (DC + "start_process", {"command": "ssh pc 'docker compose up -d'", "timeout_ms": 30000}, "ask", "p"),
    (DC + "start_process", {"command": "docker build -t x .", "timeout_ms": 30000}, "deny", "d"),
    (DC + "start_process", {"command": "Remove-Item ~/.helper-orch/guard-view.json", "shell": "powershell.exe",
                            "timeout_ms": 30000}, "deny", "s"),
    (DC + "start_process", {"command": "d`ocker --context ha ps", "timeout_ms": 30000}, "deny", "a"),
    (DC + "start_process", {"command": "ssh ha", "timeout_ms": 30000}, "deny", "b"),        # a session it types into
    (DC + "start_process", {"command": "ssh haa bash", "timeout_ms": 30000}, "deny", "b"),
    (DC + "start_process", {"command": "python3 -c \"import os; os.system('ssh pc reboot')\"",
                            "timeout_ms": 30000}, "ask", "x"),
    (DC + "start_process", {"command": "ssh pc", "timeout_ms": 30000}, "ask", "p"),
    (DC + "interact_with_process", {"pid": 4242, "input": "docker system prune -af"}, "deny", "c"),
    (DC + "interact_with_process", {"pid": 4242, "input": "ssh hbx docker rm -f web"}, "deny", "p"),
    (DC + "interact_with_process", {"pid": 4242, "input": "import os; os.system('ssh pc reboot')"}, "ask", "x"),
    ("mcp__shell__run_command", {"cmd": "docker system prune -af"}, "deny", "c"),
    ("mcp__box__exec", {"command": "rm -f ~/.helper-orch/trust.json"}, "deny", "s"),
    # a file tool's path inputs meet the Write/Edit protection (a delete or move also the setup-folder checks)
    (DC + "write_file", {"path": "~/.helper-orch/trust.json", "content": "{}"}, "deny", "s"),
    (DC + "write_file", {"path": "~/.helper-orch/ALLOW_LOCAL", "content": ""}, "deny", "e"),
    (DC + "edit_block", {"file_path": "C:/work/shop/.git/orch/config.json", "old_string": "a", "new_string": "b"},
     "deny", "s"),
    (DC + "move_file", {"source": "~/.helper-orch/guard-view.json", "destination": "C:/tmp/gv.json"}, "deny", "s"),
    (DC + "move_file", {"source": "C:/tmp/evil.json", "destination": "C:/work/shop/.git/orch/config.json"},
     "deny", "s"),
    (DC + "move_file", {"source": "~/.helper-orch", "destination": "C:/tmp/old-orch"}, "deny", "s"),
    (DC + "move_file", {"source": "C:/tmp/stage", "destination": "C:/work/shop/.git/orch"}, "deny", "s"),
    (DC + "create_directory", {"path": "~/.helper-orch/ALLOW_LOCAL"}, "deny", "e"),
    (DC + "write_pdf", {"path": "x.pdf", "outputPath": "C:/work/shop/.git/orch/state/x.pdf", "content": "# x"},
     "deny", "s"),
    ("mcp__files__delete_path", {"paths": ["C:/tmp/a", "~/.helper-orch"]}, "deny", "s"),
    ("mcp__fs__remove_file", {"path": "~/.helper-orch/trust.json"}, "deny", "s"),
    ("mcp__fs__copy_file", {"source": "C:/tmp/x.json", "destination": "C:/work/shop/.git/orch/config.json"},
     "deny", "s"),
    # any other input that names a helper or production host or a protected path asks
    (DC + "read_file", {"path": "~/.helper-orch/trust.json"}, "ask", "m"),
    ("mcp__ssh__connect", {"host": "pc"}, "ask", "m"),
    ("mcp__docker__list_containers", {"context": "orch-ha"}, "ask", "m"),
    ("mcp__http__fetch", {"url": "http://192.0.2.20:8080/admin/reset", "method": "POST"}, "ask", "m"),
    ("mcp__docker__delete_container", {"host": "hbx", "container": "web"}, "ask", "m"),
    ("mcp__notes__save", {"items": [{"text": "deploy@192.0.2.12"}]}, "ask", "m"),
    (DC + "start_process", {"command": "uptime", "host": "ha", "timeout_ms": 30000}, "ask", "m"),
    # harmless calls get no decision
    (DC + "start_process", {"command": "ssh ha uptime", "timeout_ms": 30000}, None, None),
    (DC + "start_process", {"command": "git status", "timeout_ms": 30000}, None, None),
    (DC + "start_process", {"command": "d`ocker --context ha ps", "shell": "bash", "timeout_ms": 30000}, None, None),
    (DC + "start_process", {"command": "Get-ChildItem C:/work/shop", "shell": "powershell.exe",
                            "timeout_ms": 30000}, None, None),
    (DC + "start_process", {"command": "ssh -N -L 8080:127.0.0.1:80 ha", "timeout_ms": 30000}, None, None),
    (DC + "interact_with_process", {"pid": 4242, "input": "print(1 + 1)"}, None, None),
    (DC + "write_file", {"path": "C:/work/shop/notes.md", "content": "ssh pc docker ps is read-only"}, None, None),
    (DC + "read_file", {"path": "C:/work/shop/README.md"}, None, None),
    (DC + "list_directory", {"path": "C:/work/shop/src"}, None, None),
    (DC + "start_search", {"path": "C:/work/shop", "pattern": "TODO"}, None, None),
    (DC + "kill_process", {"pid": 4242}, None, None),
    ("mcp__claude_ai_Google_Docs__read_doc", {"document_id": "abc123"}, None, None),
]

NEW_HEAVY = ["pytest -q", "python -m pytest -q", "py -m pytest", "python3 -B -m pytest tests",
             "pip install -r req.txt", "python3 -m pip install x", "uv run pytest", "uv sync",
             "uv pip install x", "bun install", "bun test", "deno test", "deno task build", "make",
             "make test", "cmake --build build", "cargo build", "cargo test", "go build ./...",
             "go test ./...", "mvn test", "gradle build", "./gradlew build"]


class Base(unittest.TestCase):
    """ORCH_HOME = a temp dir holding the sample view; cwd = the approved project root unless a test says otherwise."""
    cwd = ROOT

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("ORCH_HOME", "ORCH_LABEL", "ORCH_PREFIX")}
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        os.environ["ORCH_HOME"] = self.home
        os.environ.pop("ORCH_LABEL", None)
        os.environ.pop("ORCH_PREFIX", None)
        shutil.copy(SAMPLE, os.path.join(self.home, "guard-view.json"))
        self.view = guard.load_view()

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def prefs(self, **kw):
        with open(os.path.join(self.home, "guard.json"), "w") as f:
            json.dump(kw, f)

    def check(self, cmd, tool=B, cwd=None, view=None):
        return guard.check(cmd, tool, self.cwd if cwd is None else cwd, self.view if view is None else view)

    def warns(self):
        p = os.path.join(self.home, "guard-warn.log")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return f.read().splitlines()

    def assert_reason(self, got):
        decision, rule, reason = got
        if decision == "ask":
            self.assertTrue(reason.endswith("needs your OK") or "needs your OK (" in reason, reason)
            return
        self.assertTrue(reason.startswith("blocked: ") and "; use: " in reason, reason)
        self.assertLessEqual(len(reason), 160, reason)
        fix = reason.split("; use: ", 1)[1]
        self.assertIn(fix, {"e": [guard.USER], "p": [guard.PROD_FIX]}.get(rule, [fix]), reason)
        if rule not in ("e", "p"):  # the full orch command, or its short name when the reason needed the room
            self.assertTrue(fix.startswith(ORCH + " ") or fix.startswith("orch "), reason)

    def run_table(self, rows, decision):
        bad = []
        for row in rows:
            tool, cmd = row[0], row[1]
            want = (decision, row[2]) if decision else None
            got = self.check(cmd, tool)
            if (got and got[:2]) != want:
                bad.append("%s %r: want %s got %s" % (tool, cmd, want, got))
            elif got:
                self.assert_reason(got)
        self.assertEqual(bad, [], "\n" + "\n".join(bad))


class GuardFixtures(Base):
    def test_deny(self):
        self.run_table(DENY, "deny")

    def test_ask(self):
        self.run_table(ASK, "ask")

    def test_allow(self):
        self.run_table(ALLOW, None)

    def test_reason_names_job_from_index(self):
        for cmd, job in (("dotnet test", "api-test"), ("dotnet test --no-restore Shop.sln", "api-test"),
                         ("dotnet build -c Release", "api-build"), ("npm test", "web-test"),
                         ("npm run build", "web-build"), ("npm run lint", "web-lint"),
                         ("docker build -t shop .", "shop-image")):
            self.assertRegex(self.check(cmd)[2], r"; use: (%s|orch) run %s" % (re.escape(ORCH), re.escape(job)), cmd)
        for cmd in ("npm ci", "dotnet restore", "npx playwright test", "docker compose up -d"):
            self.assertTrue(self.check(cmd)[2].endswith("run <job> (see orch init --show)"), cmd)
        self.assertIn("run api-test --node ha", self.check("ssh ha 'cd /src && dotnet test'")[2])

    def test_allow_local_flag_waives_only_d(self):
        open(os.path.join(self.home, "ALLOW_LOCAL"), "w").close()
        for cmd in ("dotnet test", "docker build .", "npm ci", "npx playwright test"):
            self.assertIsNone(self.check(cmd), cmd)
        for cmd, rule in (("docker system prune -f", "c"), ("docker --context ha ps", "a"),
                          ("ssh ha docker ps", "b"), ("ssh ha dotnet test", "b"), ("ssh pc docker rm x", "p"),
                          ("touch ~/.helper-orch/ALLOW_LOCAL", "e")):
            self.assertEqual(self.check(cmd)[:2], ("deny", rule), cmd)

    def test_new_heavy_warn_default(self):
        for cmd in NEW_HEAVY:
            self.assertIsNone(self.check(cmd), cmd)
        self.assertEqual(len(self.warns()), len(NEW_HEAVY))
        self.prefs(new_heavy="warn")
        self.assertIsNone(self.check("cargo test"))
        self.assertEqual(len(self.warns()), len(NEW_HEAVY) + 1)
        self.assertIsNone(self.check("cargo test", cwd=OUTSIDE))  # outside every project: no warning either
        self.assertEqual(len(self.warns()), len(NEW_HEAVY) + 1)

    def test_new_heavy_deny_mode(self):
        self.prefs(new_heavy="deny")
        self.run_table([(B, c, "d") for c in NEW_HEAVY] + [(B, "ssh ha pytest", "b")], "deny")
        self.assertIsNone(self.check("pip show x"))
        self.assertIsNone(self.check("pytest -q", cwd=OUTSIDE))
        self.assertEqual(self.warns(), [])

    def test_bad_prefs_file_is_default(self):
        with open(os.path.join(self.home, "guard.json"), "w") as f:
            f.write("{oops")
        self.assertIsNone(self.check("make"))
        self.assertEqual(len(self.warns()), 1)
        self.assertIsNone(self.check("dotnet test", cwd=OUTSIDE))

    def test_prune_label_default_view_and_env(self):
        ok = "docker container prune -f --filter label=%s=1"
        self.assertIsNone(self.check(ok % "io.orch.agent"))
        self.assertEqual(self.check(ok % "io.test.x")[1], "c")
        data = sample()
        data["namespace"] = {"label": "io.shop.agent", "prefix": "shp-"}
        v = guard.View(data)
        self.assertIsNone(self.check(ok % "io.shop.agent", view=v))
        self.assertIsNone(self.check("docker buildx prune --builder shp-builder -f", view=v))
        self.assertEqual(self.check(ok % "io.orch.agent", view=v)[1], "c")
        os.environ["ORCH_LABEL"] = "io.test.x"
        os.environ["ORCH_PREFIX"] = "zz-"
        self.assertIsNone(self.check(ok % "io.test.x", view=v))
        self.assertIsNone(self.check("docker image prune -f --filter label=io.test.x=1"))
        self.assertIsNone(self.check("docker buildx prune --builder zz-builder -f"))
        self.assertEqual(self.check("docker buildx prune --builder agentx-builder -f")[1], "c")
        os.environ["ORCH_LABEL"] = "BAD LABEL!"
        self.assertIsNone(self.check(ok % "io.orch.agent"))

    def test_orch_path_from_file_location(self):
        self.assertTrue(ORCH.startswith("bash ") and ORCH.endswith("/scripts/orch"), ORCH)
        p = ORCH[5:].strip('"').replace("$HOME", "~")
        if p.startswith("~"):
            p = os.path.expanduser("~").replace("\\", "/") + p[1:]
        self.assertEqual(p.lower(), (os.path.dirname(HERE).replace("\\", "/") + "/scripts/orch").lower())

    def test_no_site_data(self):
        doc = re.compile(r"^(192\.0\.2|198\.51\.100|203\.0\.113|127\.0\.0)\.\d+$|^0\.0\.0\.0$")
        for path in (os.path.join(HERE, "guard.py"), SAMPLE):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            ips = [ip for ip in re.findall(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])", text) if not doc.match(ip)]
            self.assertEqual(ips, [], path)
            self.assertIsNone(re.search(r"[A-Za-z]:[\\/]Users[\\/]|/home/[a-z]", text), path)


class ViewDriven(Base):
    def test_classify(self):
        v = self.view
        for t, want in (("ha", "helper"), ("haa", "helper"), ("HA", "helper"), ("orch-ha", "helper"),
                        ("192.0.2.11", "helper"), ("deploy@192.0.2.11", "helper"), ("192.0.2.11:22", "helper"),
                        ("ssh://deploy@192.0.2.11:22", "helper"), ("tcp://192.0.2.11:2375", "helper"),
                        ("hb", "both"), ("hbx", "both"), ("orch-hb", "both"), ("192.0.2.12", "both"),
                        ("ssh://deploy@192.0.2.12:2222/", "both"), ("[192.0.2.12]:2222", "both"),
                        ("pc", "prod"), ("deploy@192.0.2.20", "prod"), ("ssh://192.0.2.20", "prod"),
                        ("198.51.100.7", None), ("ssh://198.51.100.7", None), ("unix:///var/run/docker.sock", None),
                        ("npipe:////./pipe/docker_engine", None), ("default", None), ("", None), (None, None)):
            self.assertEqual(guard.classify(t, v), want, t)
        self.assertEqual(guard.classify("haa"), "helper")  # no view given: loads $ORCH_HOME/guard-view.json
        os.remove(os.path.join(self.home, "guard-view.json"))
        guard.CTX = None
        self.assertIsNone(guard.classify("haa"))

    def test_v1_defect_any_ssh_url_is_not_a_helper(self):
        for cmd in ("docker -H ssh://198.51.100.7 ps", "docker -H ssh://deploy@198.51.100.7 build .",
                    "DOCKER_HOST=ssh://198.51.100.7 docker compose up -d", "docker context use other",
                    "docker context create x --docker host=ssh://198.51.100.7"):
            self.assertIsNone(self.check(cmd), cmd)

    def test_alias_and_port_resolution(self):
        data = sample()
        data["hosts"] += [{"name": "n1", "roles": ["helper"], "hostname": "192.0.2.30", "port": 22},
                          {"name": "n2", "roles": ["prod"], "hostname": "192.0.2.30", "port": 2200}]
        v = guard.View(data)
        self.assertEqual(self.check("ssh -p 2200 192.0.2.30 docker rm x", view=v)[:2], ("deny", "p"))
        self.assertEqual(self.check("ssh -p 22 192.0.2.30 docker ps", view=v)[:2], ("deny", "b"))
        self.assertEqual(self.check("ssh -o Port=22 192.0.2.30 docker ps", view=v)[:2], ("deny", "b"))
        self.assertIsNone(self.check("ssh 192.0.2.30 docker ps", view=v))  # port unknown: production wins
        self.assertIsNone(self.check("ssh haa uptime"))
        self.assertEqual(self.check("ssh HAA docker ps")[:2], ("deny", "b"))
        self.assertIn("production hb (hbx)", self.check("scp x hbx:/tmp")[2])
        self.assertIn("production pc (deploy@192.0.2.20)", self.check("ssh 192.0.2.20 touch x")[2])

    def test_host_without_known_role_is_ignored(self):
        v = guard.View({"schema": 1, "hosts": [{"name": "x1", "roles": ["other"], "hostname": "192.0.2.40"}]})
        self.assertIsNone(self.check("ssh x1 docker rm -f web", view=v))

    def test_view_shapes(self):
        for bad in ([], {"schema": 2}, {"schema": 1, "hosts": {}}, {"schema": 1, "hosts": [{"roles": ["helper"]}]},
                    {"schema": 1, "hosts": [{"name": "a", "roles": ["helper"], "port": "22"}]},
                    {"schema": 1, "hosts": [{"name": "a", "roles": "helper", "aliases": [1]}]},
                    {"schema": 1, "projects": [{"store": "C:/x"}]},
                    {"schema": 1, "projects": [{"root": "C:/x", "jobs": ["a"]}]},
                    {"schema": 1, "namespace": "io.x"}):
            self.assertRaises(guard.ViewError, guard.View, bad)
        v = guard.View({"schema": 1})
        self.assertEqual((v.keys, v.projects, v.label, v.prefix), ({}, [], None, None))
        v = guard.View({"schema": 1, "projects": [{"root": "C:\\Work\\Shop", "deploy": "./d.sh", "rollback": None}]})
        self.assertEqual((v.projects[0]["root"], v.projects[0]["commands"]), ("c:/work/shop", ["./d.sh"]))

    def test_load_view(self):
        p = os.path.join(self.home, "guard-view.json")
        self.assertIsInstance(guard.load_view(), guard.View)
        for text in ("", "{oops", "[]", "{\"schema\": 1, \"hosts\": 5}", "\xff\xfe"):
            with open(p, "w", encoding="latin-1") as f:
                f.write(text)
            self.assertRaises(guard.ViewError, guard.load_view)
        os.remove(p)
        self.assertIsNone(guard.load_view())
        os.mkdir(p)
        self.assertRaises(guard.ViewError, guard.load_view)

    def test_rule_d_scope(self):
        out = OUTSIDE
        for cmd in ("dotnet test", "npm ci", "docker build .", "git -C C:/work/shop status && npm ci",
                    "cd C:/work/shopping && npm ci", "npm --prefix web ci"):
            self.assertIsNone(self.check(cmd, cwd=out), cmd)
            self.assertIsNone(guard.check(cmd, B, None, self.view), cmd)  # payload without cwd
        for cmd in ("cd C:/work/shop && npm ci", "cd /c/work/shop/web && npm ci", "cd ../shop && dotnet build",
                    "pushd C:\\\\work\\\\shop; npm test", "npm --prefix C:/work/shop/web ci",
                    "dotnet test C:/work/shop/api/Api.sln", "docker build C:/work/shop",
                    "yarn --cwd=/mnt/c/work/shop/web build", "cd C:/WORK/SHOP/ && npm ci"):
            self.assertEqual(self.check(cmd, cwd=out)[:2], ("deny", "d"), cmd)
        self.assertEqual(self.check("Set-Location C:\\work\\shop; dotnet test", P, cwd=out)[:2], ("deny", "d"))
        for cwd in (ROOT, ROOT + "/web", "c:\\WORK\\Shop\\api", "/c/work/shop", "C:/work/shop/"):
            self.assertEqual(self.check("npm ci", cwd=cwd)[:2], ("deny", "d"), cwd)
        self.prefs(heavy_scope="everywhere")
        self.assertEqual(self.check("dotnet test", cwd=out)[:2], ("deny", "d"))
        self.assertEqual(guard.check("npm ci", B, None, self.view)[:2], ("deny", "d"))
        self.assertTrue(guard.check("npm ci", B, None, guard.View({"schema": 1}))[2].endswith("init --show)"))

    def test_deploy_mask(self):
        m = guard._mask("cd C:/work/shop && ./scripts/deploy.sh; ssh hbx  /srv/shop/rollback.sh", self.view)
        self.assertEqual(m, "cd C:/work/shop && true; true")
        m = "./scripts/deploy.sh --now; FOO=1 ./scripts/deploy.sh; xargs ssh hbx /srv/shop/rollback.sh < a"
        self.assertEqual(guard._mask(m, self.view), m)  # extra arguments or a prefix: not the registered command
        self.assertEqual(guard._mask("./scripts/deploy.sh.bak", self.view), "./scripts/deploy.sh.bak")
        self.assertEqual(guard._mask("x./scripts/deploy.sh", self.view), "x./scripts/deploy.sh")

    def test_ask_reason_lists_each_change_once(self):
        got = self.check("ssh pc touch a; ssh pc touch b; " + ORCH + " init --approve")
        self.assertEqual(got[:2], ("ask", "p"))
        self.assertEqual(got[2], "'touch' on production pc (deploy@192.0.2.20) needs your OK; " + guard.APPROVE_REASON)
        self.assertEqual(self.check(ORCH + " init --approve")[2], "saving the orch setup needs your OK")

    def test_deny_beats_ask(self):
        self.assertEqual(self.check("ssh pc touch a && docker system prune -af")[:2], ("deny", "c"))
        self.assertEqual(self.check(ORCH + " init --approve && npm ci")[:2], ("deny", "d"))

    def test_setup_files_in_orch_home(self):
        h = self.home.replace("\\", "/")
        for cmd in ("echo '{}' > %s/trust.json" % h, "cp x %s/guard-view.json" % h, "rm %s/registry.json" % h,
                    "rm -rf %s" % h, "sed -i s/1/2/ %s/trust.json" % h):
            self.assertEqual(self.check(cmd)[:2], ("deny", "s"), cmd)
        for cmd in ("cat %s/trust.json" % h, "ls %s" % h, "rm -rf %s/results/x1" % h, "cat %s/guard-warn.log" % h):
            self.assertIsNone(self.check(cmd), cmd)
        self.assertTrue(self.check("echo x > %s/trust.json" % h)[2].endswith(ORCH + " init"))

    def test_file_tools(self):
        h = self.home.replace("\\", "/")
        rows = [("C:\\ProgramData\\docker\\config\\daemon.json", "c"), ("/home/x/.helper-orch/guard.json", "e"),
                ("~/.helper-orch/ALLOW_LOCAL", "e"), ("C:\\work\\shop\\.git\\orch\\config.json", "s"),
                ("c:/WORK/shop/.git/orch/state/runs.jsonl", "s"), (".git/orch/config.json", "s"),
                (h + "/guard-view.json", "s"), (h + "/trust.json", "s"), (h + "/registry.json", "s"),
                ("C:/work/shop/src/app.py", None), ("hooks/test_guard.json", None), ("C:/work/shop/config.json", None),
                (h + "/notes.txt", None), ("", None)]
        for tool, key in (("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"),
                          ("NotebookEdit", "notebook_path")):
            for path, rule in rows:
                got = guard.decide({"tool_name": tool, "tool_input": {key: path}, "cwd": ROOT}, self.view)
                self.assertEqual(got and got[:2], rule and ("deny", rule), (tool, path))
                if got:
                    self.assert_reason(got)


class Findings(Base):
    """Targeted checks for review-round-1 guard findings; each would pass (wrongly) without the matching fix."""

    def test_r20_ssh_options_after_destination(self):
        # OpenSSH permutes options placed after the host; the guard must still classify them
        self.assertEqual(self.check("ssh haa -t docker system prune -af --volumes")[:2], ("deny", "b"))
        self.assertEqual(self.check("ssh deploy@192.0.2.11 -p 22 docker rm -f web")[:2], ("deny", "b"))
        self.assertEqual(self.check("ssh anyname -o HostName=192.0.2.11 docker ps")[:2], ("deny", "b"))
        self.assertEqual(self.check("ssh pc -q rm -rf /srv/shop")[:2], ("deny", "p"))
        # `--` before the host stops option parsing: `-t ls` is the remote command, not a tty flag
        self.assertEqual(self.check("ssh -- pc -t ls")[:2], ("ask", "p"))
        self.assertEqual(self.check("ssh -- pc docker rm -f web")[:2], ("deny", "p"))
        self.assertIsNone(self.check("ssh pc -N -L 8080:127.0.0.1:80"))

    def test_r25_ssh_o_commands(self):
        self.assertEqual(self.check("ssh -o RemoteCommand='docker rm -f web' haa")[:2], ("deny", "b"))
        self.assertEqual(self.check("ssh -o 'RemoteCommand=rm -rf /srv/shop' pc")[:2], ("deny", "p"))
        self.assertEqual(self.check("ssh -o RemoteCommand='docker system prune -af' other-host")[:2], ("deny", "c"))
        self.assertEqual(self.check("ssh -o LocalCommand='docker system prune -af' haa uptime")[:2], ("deny", "c"))
        self.assertEqual(self.check("ssh -o ProxyCommand='docker volume prune -f' box uptime")[:2], ("deny", "c"))
        self.assertIsNone(self.check("ssh -o RemoteCommand=uptime pc"))      # read-only remote: allowed, not asked

    def test_r21_cmd_double_slash(self):
        for cmd, rule in (('cmd //c "docker --context orch-ha ps"', "a"), ('cmd //d //s //c "ssh pc rm -rf /x"', "p"),
                          ("cmd.exe //c dotnet test", "d"), ('cmd //c "echo 1 > ~/.helper-orch/ALLOW_LOCAL"', "e")):
            self.assertEqual(self.check(cmd)[:2], ("deny", rule), cmd)
        self.assertIsNone(self.check('cmd //c "docker ps"'))

    def test_r22_trailing_dot_setup_files(self):
        h = self.home.replace("\\", "/")
        for cmd in ("echo '{}' > %s/trust.json." % h, "echo '{}' > '%s/trust.json. '" % h,
                    "echo '{}' > %s/guard-view.json." % h,
                    "echo '{}' > C:/work/shop/.git/orch/config.json.",
                    "echo '{}' > C:/work/shop/.git/orch./config.json",
                    "python -c \"open('C:/work/shop/.git/orch/config.json.','w')\""):
            self.assertEqual(self.check(cmd)[:2], ("deny", "s"), cmd)
        for cmd in ("Set-Content -Path %s\\trust.json. -Value '{}'" % self.home,
                    "Out-File -FilePath C:/work/shop/.git/orch/config.json."):
            self.assertEqual(self.check(cmd, P)[:2], ("deny", "s"), cmd)
        self.assertIsNone(self.check("Set-Content C:/work/shop/.git/orch/config.json.bak x", P))  # .bak is not a setup file

    def test_r23_glob_and_find_delete(self):
        h = self.home.replace("\\", "/")
        for cmd in ("rm -f %s/*" % h, "rm -f %s/*.json" % h, "rm -f %s/{guard-view,trust}.json" % h,
                    "find %s -delete" % h, "find %s -name guard-view.json -delete" % h,
                    "find %s -exec rm {} +" % h, "rm -f C:/work/shop/.git/orch/*",
                    "find C:/work/shop/.git/orch -delete"):
            self.assertEqual(self.check(cmd)[:2], ("deny", "s"), cmd)
        self.assertEqual(self.check("Remove-Item %s/*" % h, P)[:2], ("deny", "s"))
        # deleting the guard view really does disable the guard, so it must be blocked; prove a near-miss is allowed
        self.assertIsNone(self.check("find %s -name '*.json'" % h))
        self.assertIsNone(self.check("rm -f %s-notes/*" % h))

    def test_r24_powershell_braced_docker_host(self):
        for cmd, rule in (("${env:DOCKER_HOST}='ssh://haa'; docker rm -f web", "a"),
                          ("${env:DOCKER_CONTEXT}='orch-ha'; docker ps", "a"),
                          ("Set-Item -Path env:DOCKER_HOST -Value ssh://haa; docker ps", "a"),
                          ("${env:DOCKER_HOST}='ssh://hbx'; docker rm -f web", "p")):
            self.assertEqual(self.check(cmd, P)[:2], ("deny", rule), cmd)
        self.assertIsNone(self.check("${env:DOCKER_HOST}='ssh://198.51.100.7'; docker ps", P))

    def test_r26_copy_destination_options(self):
        for cmd in ("cp -t C:/work/shop/.git/orch /tmp/evil/config.json",
                    "cp --target-directory=C:/work/shop/.git/orch x.json",
                    "install -t C:/work/shop/.git/orch x.json", "cp -t %s /tmp/evil/trust.json" % self.home,
                    "robocopy C:/tmp/stage C:/work/shop/.git/orch config.json",
                    "xcopy C:/tmp/stage/config.json C:/work/shop/.git/orch/ /Y"):
            self.assertEqual(self.check(cmd)[:2], ("deny", "s"), cmd)
        for cmd in ("Copy-Item -Destination C:\\work\\shop\\.git\\orch -Path C:\\tmp\\stage\\config.json",
                    "Copy-Item -Destination C:/work/shop/.git/orch/config.json -Path C:/tmp/stage/config.json"):
            self.assertEqual(self.check(cmd, P)[:2], ("deny", "s"), cmd)
        self.assertIsNone(self.check("cp -t /tmp/backup C:/work/shop/.git/orch/config.json"))  # copying OUT is read-only

    def test_r27_scan_skips_inert_data(self):
        # orch owns its journal (stdin is data), and a commit/PR message is text, even when it names a known host
        for cmd in (ORCH + " journal add <<'EOF'\n{\"detail\":\"old DOCKER_HOST=ssh://deploy@192.0.2.11\"}\nEOF",
                    "git commit -m 'docs: ALLOW_LOCAL and DOCKER_HOST=ssh://haa'",
                    "gh pr create --body 'set DOCKER_CONTEXT=orch-ha then run'"):
            self.assertIsNone(self.check(cmd), cmd)
        # but a real env write (to a shell profile, not orch) is still caught
        self.assertEqual(self.check("cat >> ~/.bashrc <<'EOF'\nexport DOCKER_HOST=ssh://ha\nEOF")[:2], ("deny", "a"))

    def test_r28_brace_tokens(self):
        for cmd in ("ssh pc docker inspect -f '{{.State.Health.Status}}' web",
                    "ssh pc 'docker ps --format {{.Names}}'", "ssh pc docker ps --format '{{.Names}} {{.Status}}'"):
            self.assertIsNone(self.check(cmd), cmd)      # read-only prod template: no needless ask
        self.assertEqual(self.check("ssh pc true {{x}} '; rm -rf /srv/shop'")[:2], ("deny", "p"))  # bypass closed
        self.assertEqual(self.check("ssh pc echo { '; rm -rf /srv/shop'")[:2], ("deny", "p"))
        self.assertEqual(self.check("ssh ha true {{x}} '; docker ps'")[:2], ("deny", "b"))

    def test_r29_dir_hints_scope_rule_d(self):
        out = OUTSIDE
        for cmd in ("env -C C:/work/shop/web npm test", "env --chdir=C:/work/shop/api dotnet test",
                    "env -C /c/work/shop/web npm ci", "sudo -D C:/work/shop/api dotnet test",
                    "sudo --chdir=C:/work/shop npm ci"):
            self.assertEqual(self.check(cmd, cwd=out)[:2], ("deny", "d"), cmd)
        for cmd in ("Start-Process dotnet -ArgumentList test -WorkingDirectory C:/work/shop/api -Wait",
                    "Start-Process -FilePath dotnet -ArgumentList 'test' -WorkingDirectory C:\\work\\shop\\api",
                    "pwsh -WorkingDirectory C:/work/shop/api -Command 'dotnet test'",
                    "pwsh -wd C:\\work\\shop\\web -c 'npm test'"):
            self.assertEqual(self.check(cmd, P, cwd=out)[:2], ("deny", "d"), cmd)
        # the hint is for one command only: a plain cd-equivalent must not leak to a later command
        self.assertIsNone(self.check("env -C C:/work/shop ls; npm test", cwd=out))

    def test_r39_approve_entry_points(self):
        for cmd in ("python scripts/orch_init.py --approve", "python -B scripts/orch_init.py --approve --offline",
                    "python -m orch init --approve", "cd scripts && python -m orch init --approve",
                    "PYTHONPATH=scripts python -m orch init --approve",
                    "py scripts/orch_init.py --project C:/work/shop --appr"):
            self.assertEqual(self.check(cmd)[:2], ("ask", "i"), cmd)
        self.assertEqual(self.check("python -m orch init --approve", P)[:2], ("ask", "i"))
        self.assertIsNone(self.check("python scripts/orch_init.py --show"))   # non-approval entry: passes unanalysed

    def test_r40_user_only_env_vars(self):
        for var in ("ORCH_HOME", "ORCH_CONFIG", "ORCH_CI_CHILD", "ORCH_FAULT", "ORCH_SELFTEST", "ORCH_TEST_MODE", "ORCH_GC_NODE_JSON"):
            self.assertEqual(self.check("%s=/tmp/x %s run api-test" % (var, ORCH))[:2], ("deny", "e"), var)
            self.assertEqual(self.check("export %s=/tmp/x" % var)[:2], ("deny", "e"), var)
            self.assertEqual(self.check("$env:%s='/tmp/x'; %s run api-test" % (var, ORCH), P)[:2], ("deny", "e"), var)
            self.assertEqual(self.check("${env:%s}='/tmp/x'; %s ci" % (var, ORCH), P)[:2], ("deny", "e"), var)
            self.assertEqual(self.check("setx %s C:/tmp/x" % var, P)[:2], ("deny", "e"), var)
            self.assertEqual(self.check("Set-Item -Path env:%s -Value /tmp/x" % var, P)[:2], ("deny", "e"), var)
            self.assertEqual(self.check("[Environment]::SetEnvironmentVariable('%s','/tmp/x','User')" % var, P)[:2],
                             ("deny", "e"), var)
        for cmd in ("env ORCH_HOME=/tmp/x bash scripts/orch run api-test", "declare -x ORCH_CONFIG=/tmp/x",
                    "typeset ORCH_FAULT=create@n1", "readonly ORCH_SELFTEST=1"):
            self.assertEqual(self.check(cmd)[:2], ("deny", "e"), cmd)
        for cmd in ("Set-Item env:ORCH_HOME C:/tmp/x", "New-Item -Path Env:ORCH_CONFIG -Value /tmp/x"):
            self.assertEqual(self.check(cmd, P)[:2], ("deny", "e"), cmd)
        self.assertIsNone(self.check("echo $ORCH_HOME"))                         # reading is fine
        self.assertIsNone(self.check("ORCH_HOMEX=/tmp/x bash scripts/orch run api-test"))  # similarly-named var, not ours
        self.assertIsNone(self.check("ORCH_PYTHON=python3 bash scripts/orch run api-test"))  # not a user-only var


class Fix2(Base):
    """v2 fix 2 (real agent runs): variable command names (rule v) and project job tools (rule d)."""

    def test_v_real_agent_commands(self):
        # each of these got no decision before the fix
        self.assertEqual(self.check('O="bash C:/work/skills/helper-node-orchestrator/scripts/orch"; '
                                    '$O init --approve | tail -1')[:2], ("ask", "i"))
        self.assertEqual(self.check('X="ssh hbx"; $X docker rm -f web')[:2], ("deny", "p"))
        self.assertEqual(self.check("O=orch; ${O} init --approve")[:2], ("ask", "i"))
        self.assertEqual(self.check('$O = "bash C:/work/x/orch"; & $O init --approve', P)[:2], ("ask", "i"))

    def test_v_orch_journal_pattern(self):
        o = 'O="bash C:/work/skills/helper-node-orchestrator/scripts/orch"; '
        pat = (o + "$O journal start <<'EOF'\n{\"summary\": \"export DOCKER_HOST=ssh://hbx; HELPER_ORCH_LOCAL=1\", "
               "\"request\": \"ssh hbx docker rm -f web\"}\nEOF\n$O init")
        self.assertIsNone(self.check(pat))
        self.assertEqual(self.check(pat + " --approve")[:2], ("ask", "i"))
        self.assertIsNone(self.check(o + "$O run api-test --node ha -- npm test"))   # orch's own args are data
        for cmd in ("O=orch; echo $O init --approve", 'echo "$O init --approve"', "D=docker; ls $D",
                    "D=docker; $D ps"):
            self.assertIsNone(self.check(cmd), cmd)

    def test_v_unresolved_asks(self):
        for cmd in ("$O init --approve", "O=$(command -v orch); $O init --approve", "$X hbx docker rm -f web",
                    "$D system prune -af", "$D build -t x .", "\"$(command -v docker)\" build ."):
            got = self.check(cmd)
            self.assertEqual(got[:2], ("ask", "v"), cmd)
            self.assertIn("spell the command out", got[2])
        self.assertIn("comes from $(...)", self.check("$(command -v orch) init --approve")[2])
        # approvals, known hosts and prune ask anywhere; heavy words follow rule d's scope and ALLOW_LOCAL
        self.assertIsNone(self.check("$D build -t x .", cwd=OUTSIDE))
        self.assertIsNone(self.check("$PY -m pytest -q", cwd=OUTSIDE))
        for cmd in ("$D system prune -af", "$O init --approve", "$X hbx docker rm -f web"):
            self.assertEqual(self.check(cmd, cwd=OUTSIDE)[:2], ("ask", "v"), cmd)
        self.assertEqual(self.check("cd C:/work/shop/web && $D build -t x .", cwd=OUTSIDE)[:2], ("ask", "v"))
        self.assertEqual(self.check("cd C:/work/lab && $D build .", cwd=OUTSIDE)[:2], ("ask", "v"))
        self.prefs(heavy_scope="everywhere")
        self.assertEqual(self.check("$D build -t x .", cwd=OUTSIDE)[:2], ("ask", "v"))
        open(os.path.join(self.home, "ALLOW_LOCAL"), "w").close()
        self.assertIsNone(self.check("$D build -t x ."))
        self.assertEqual(self.check("$O init --approve")[:2], ("ask", "v"))
        # nothing relevant in the rest: no decision, as before
        for cmd in ("$EDITOR notes.md", "$PAGER build.log", "\"$X\" --version", "$O init", "$O journal start",
                    "$O run api-test"):
            self.assertIsNone(self.check(cmd), cmd)

    def test_v_values_and_setters(self):
        rows = (("D=docker; D=podman; $D build .", "d"), ("unset D; D=docker; $D build .", "d"),
                ("O=echo; O=$(cat f); $O docker build .", "v"), ("D=echo; mapfile -t D < f; $D build .", "v"),
                ("D=echo; printf -v D %s docker; $D build .", "v"), ("D=echo; getopts ab D; $D build .", "v"),
                ("D=echo; unset D; $D docker build .", "v"), ("D=echo; eval \"$X\"; $D docker build .", "v"),
                ("for D in $(ls); do $D build .; done", "v"), ("declare -a D=(docker); $D build .", "v"),
                ("D=echo; D+=docker; $D build .", "v"), ("O='$O'; $O init --approve", "v"))
        for cmd, rule in rows:
            self.assertEqual(self.check(cmd)[:2], ("deny" if rule != "v" else "ask", rule), cmd)
        for cmd, rule in (("[string]$D = 'docker'; & $D build .", "d"), ("$D='docker'; & $D system prune", "c"),
                          ("New-Variable -Name D -Value docker -Force; & $D build .", "d"),
                          ("$D = 'echo'; Remove-Variable D; & $D build .", "d")):
            self.assertEqual(self.check(cmd, P)[:2], ("deny", rule), cmd)
        self.assertIsNone(self.check("$x = 'C:/work/other'; Set-Location $x", P))

    def test_v_expansion_is_bounded(self):
        # self-referencing values multiply the alternatives at every level (2^9 checks here, 8^9 with four such
        # assignments) unless the per-check expansion budget stops them
        calls, orig = [0], guard.check_cmd

        def counting(*a, **kw):
            calls[0] += 1
            return orig(*a, **kw)
        guard.check_cmd = counting
        try:
            got = self.check("X='$X a'; X='$X b'; $X build .")
        finally:
            guard.check_cmd = orig
        self.assertLessEqual(calls[0], 80)
        self.assertEqual(got[:2], ("ask", "v"))       # the budget runs out: cannot be told, and build is heavy
        self.assertEqual(self.check("X='$X a'; X='$X b'; X='$X c'; X='$X d'; $X build .")[:2], ("ask", "v"))

    def test_d_project_job_tools(self):
        lab = "C:/work/lab"
        for cmd, job in (("python -m pytest -q", "py-test"), ("pytest -q tests", "py-test"),
                         ("python3 -B -m pytest", "py-test"), ("uv run pytest", "py-test"),
                         ("python -m unittest discover", "py-unit"), ("ruff check .", "py-lint"),
                         ("python -m ruff format --check", "py-lint"), ("npx jest --ci", "js-test"),
                         ("pnpm exec jest", "js-test"), ("yarn typecheck", "web-types"),
                         ("npm run typecheck", "web-types"), ("bun run typecheck", "web-types")):
            got = self.check(cmd, cwd=lab + "/src")
            self.assertEqual(got[:2], ("deny", "d"), cmd)
            self.assertRegex(got[2], r"; use: (%s|orch) run %s$" % (re.escape(ORCH), re.escape(job)), got)
            self.assert_reason(got)
        self.assertIn("blocked: pytest $(...) on master", self.check("pytest $(cat list)", cwd=lab)[2])  # no NUL
        # another project's jobs: unchanged (warn only in C:/work/shop, nothing outside every approved root)
        for cwd in (ROOT, OUTSIDE):
            for cmd in ("python -m pytest -q", "ruff check .", "npm run typecheck"):
                self.assertIsNone(self.check(cmd, cwd=cwd), (cwd, cmd))
        self.prefs(heavy_scope="everywhere")
        self.assertIsNone(self.check("ruff check .", cwd=OUTSIDE))
        for cmd in ("pytest --version", "python -m pip list", "bun test", "mypy ."):
            self.assertIsNone(self.check(cmd, cwd=lab), cmd)
        self.prefs(new_heavy="deny")
        self.assertTrue(self.check("pytest -q", cwd=lab)[2].endswith(" run py-test"))
        open(os.path.join(self.home, "ALLOW_LOCAL"), "w").close()
        self.assertIsNone(self.check("python -m pytest -q", cwd=lab))

    def test_d_job_index_canonical_forms(self):
        def forms(cmd):
            return guard._canon(cmd.split())
        self.assertIn(["pytest", "-q"], forms("python -B -m pytest -q"))
        self.assertIn(["pytest"], forms("py.test"))
        self.assertIn(["pytest"], forms("python -mpytest"))
        self.assertIn(["jest", "--ci"], forms("npx -y jest --ci"))
        self.assertIn(["jest"], forms("pnpm --filter web exec jest"))
        self.assertIn(["pytest", "tests"], forms("uv run --with x pytest tests"))
        self.assertIn(["run", "lint"], forms("yarn lint"))
        self.assertIn(["run", "lint"], forms("bun run lint"))
        self.assertIn(["run", "test"], forms("pnpm test"))
        self.assertNotIn(["run", "test"], forms("bun test"))       # bun's own test runner, not a package script
        self.assertNotIn(["run", "install"], forms("yarn install"))
        self.assertEqual(guard._canon([]), [])
        self.assertIn("run web-test", self.check("pnpm test")[2])   # the job is named through the launcher too


class Fix2c(Base):
    """v2 fix 2c (review round 2 guard findings Q20-Q23 and the host/path variable open item)."""

    def test_q20_blank_by_span_not_string(self):
        # a data word (orch arg, commit/PR message) must not hide a real assignment that repeats its text
        for cmd, rule in (("git commit --allow-empty -q -m docker_host; export DOCKER_HOST=ssh://ha; docker ps", "a"),
                          ("git commit -q -m DOCKER_HOST; export DOCKER_HOST=tcp://192.0.2.20:2375; docker rm -f web",
                           "p"),
                          ("git commit -q -m orch_config; export ORCH_CONFIG=/tmp/legacy", "e"),
                          (ORCH + " journal note docker_host; export DOCKER_HOST=ssh://ha; docker ps", "a"),
                          ("gh pr create -b docker_context; export DOCKER_CONTEXT=orch-ha; docker ps", "a"),
                          ("git commit -q -m docker_host; cat >> ~/.bashrc <<'EOF'\nexport DOCKER_HOST=ssh://ha\nEOF",
                           "a"),
                          ('git commit -q -m DOCKER_HOST""=ssh://ha; export DOCKER_HOST=ssh://ha; docker ps', "a")):
            self.assertEqual(self.check(cmd)[:2], ("deny", rule), cmd)
        self.assertEqual(self.check("git commit -q -m docker_host; $env:DOCKER_HOST='ssh://deploy@192.0.2.11'; "
                                    "docker ps", P)[:2], ("deny", "a"))
        # near-miss: a message or orch journal body that only mentions a token is still inert
        self.assertIsNone(self.check("git commit -m 'docs: mention ORCH_HOME and ORCH_CONFIG'"))
        self.assertIsNone(self.check(ORCH + " journal add <<'EOF'\n{\"d\":\"DOCKER_HOST=ssh://ha\"}\nEOF"))

    def test_q21_env_append_default_coalesce_spellings(self):
        for var in ("ORCH_HOME", "ORCH_CONFIG", "ORCH_TEST_MODE", "ORCH_CI_CHILD", "ORCH_FAULT", "ORCH_SELFTEST", "ORCH_GC_NODE_JSON"):
            self.assertEqual(self.check("%s+=/tmp/x %s run t" % (var, ORCH))[:2], ("deny", "e"), var)
            self.assertEqual(self.check("$env:%s += '/x'; %s run t" % (var, ORCH), P)[:2], ("deny", "e"), var)
            self.assertEqual(self.check("${env:%s}+='1'; %s ci" % (var, ORCH), P)[:2], ("deny", "e"), var)
            self.assertEqual(self.check("$env:%s ??= '/x'; %s run t" % (var, ORCH), P)[:2], ("deny", "e"), var)
        for cmd, rule in (("export DOCKER_HOST+=ssh://ha; docker ps", "a"),
                          ("export DOCKER_CONTEXT+=orch-ha; docker ps", "a"),
                          ("${DOCKER_HOST:=ssh://ha}; docker ps", "a")):
            self.assertEqual(self.check(cmd)[:2], ("deny", rule), cmd)
        self.assertEqual(self.check("$env:DOCKER_HOST += 'ssh://deploy@192.0.2.20'; docker compose -p shop down",
                                    P)[:2], ("deny", "p"))
        self.assertIsNone(self.check("ORCH_HOMEX+=/tmp/x bash scripts/orch run api-test"))  # not our variable
        self.assertIsNone(self.check("echo $ORCH_HOME"))

    def test_q22_approve_entry_points_extended(self):
        for cmd in ("python -morch init --approve", "python -morch_init --approve", "python -Bmorch init --approve",
                    "python -X utf8 -morch init --approve", "python -Imorch init --approve",
                    "python --check-hash-based-pycs never -m orch init --approve",
                    "python -m runpy orch init --approve", "python -m runpy orch_init --appr",
                    "python -c \"import runpy; runpy.run_path('scripts/orch_init.py'); approve\""):
            self.assertEqual(self.check(cmd)[:2], ("ask", "i"), cmd)
        self.assertEqual(self.check("python -Imorch init --approve", P)[:2], ("ask", "i"))
        for cmd in ("python -morch run api-test", "python -m runpy orch run api-test",
                    "python -c 'import runpy; runpy.run_module(\"orch\")'"):
            self.assertIsNone(self.check(cmd), cmd)      # a non-approval orch entry passes unanalysed

    def test_q23_wrapper_programs_unwrapped(self):
        for cmd, rule in (("winpty ssh deploy@192.0.2.20 docker rm -f app", "p"),
                          ("flock /tmp/l ssh pc docker rm -f app", "p"),
                          ("flock /tmp/l -c 'ssh pc docker rm -f app'", "p"),
                          ("strace -f docker --context ha ps", "a"), ("ltrace docker --context ha ps", "a"),
                          ("strace -o /dev/null docker system prune -af", "c"),
                          ("runuser -u deploy -- ssh pc rm -rf /srv/shop", "p"),
                          ("runuser -u deploy -c 'docker system prune -af'", "c"),
                          ("script -qc 'ssh pc rm -rf /srv/shop' /dev/null", "p"),
                          ("sg docker -c 'docker volume prune -f'", "c"),
                          ("systemd-run --scope --uid 1000 docker system prune -af", "c"),
                          ("pkexec docker system prune -af", "c"), ("caffeinate docker system prune -af", "c"),
                          ("parallel docker system prune -af ::: a", "c"),
                          ("ssh haa flock /tmp/l docker rm -f x", "b"), ("ssh haa strace -f docker rm -f x", "b"),
                          ("winpty " + ORCH + " init --approve", "i")):
            self.assertEqual(self.check(cmd)[:2], ("deny" if rule != "i" else "ask", rule), cmd)
        # an unknown program cloaking a sensitive command asks (it is not hard-denied; it may not be a wrapper)
        for cmd in ("myprog ssh pc docker rm -f web", "myprog ssh ha docker ps",
                    "unknownwrap " + ORCH + " init --approve"):
            self.assertEqual(self.check(cmd)[:2], ("ask", "v"), cmd)
        for cmd in ("winpty ssh ha uptime", "winpty docker ps", "flock /tmp/l ls", "strace -f git status",
                    "myprog cat /etc/hosts", "ssh ha journalctl --no-pager -n 100 -u docker"):
            self.assertIsNone(self.check(cmd), cmd)      # wrapper/leaf around a benign command stays allowed

    def test_v_host_and_path_variables_substituted(self):
        for cmd, rule in (("H=hbx; ssh $H docker rm -f web", "p"), ("H=ha; ssh $H docker ps", "b"),
                          ("H=192.0.2.20; ssh $H docker rm -f web", "p"), ("H=pc; ssh $H sudo reboot", "p"),
                          ("H=ha; docker -H ssh://$H ps", "a"), ("D=orch-ha; docker --context $D ps", "a"),
                          ("D=~/.helper-orch; echo x > $D/trust.json", "s"),
                          ("T=~/.helper-orch/trust.json; rm -f $T", "s")):
            self.assertEqual(self.check(cmd)[:2], ("deny", rule), cmd)
        self.assertEqual(self.check("P=hbx; scp dist.tar $P:/srv/shop")[:2], ("ask", "p"))
        self.assertEqual(self.check("$H='hbx'; docker -H ssh://$H rm -f web", P)[:2], ("deny", "p"))
        # an unresolvable host toward a mutating command asks "spell the host out"; a read-only one does not
        for cmd in ("H=$(cat f); ssh $H docker rm -f web", "P=$(cat f); scp a.tar $P:/srv"):
            got = self.check(cmd)
            self.assertEqual(got[:2], ("ask", "v"), cmd)
            self.assertIn("spell the host out", got[2])
        for cmd in ("H=ha; ssh $H uptime", "H=$(cat f); ssh $H uptime", "echo $H", "H=hbx; echo deploying to $H"):
            self.assertIsNone(self.check(cmd), cmd)


class Fix3(Base):
    """Council 2 must-fixes: MF2 local substitutions in ssh, MF3 MCP tools, MF4 code the guard cannot read, and the
    orch-* docker contexts."""

    def test_mf3_mcp_table(self):
        bad = []
        for tool, ti, decision, rule in MCP:
            got = guard.decide({"tool_name": tool, "tool_input": ti, "cwd": ROOT}, self.view)
            if (got and got[:2]) != ((decision, rule) if decision else None):
                bad.append("%s %r: want %s got %s" % (tool, ti, (decision, rule), got))
            elif got:
                self.assert_reason(got)
        self.assertEqual(bad, [], "\n" + "\n".join(bad))

    def test_mf3_mcp_reasons_and_dialects(self):
        d = guard.decide({"tool_name": "mcp__ssh__connect", "tool_input": {"host": "hbx"}}, self.view)
        self.assertEqual(d[:2], ("ask", "m"))
        self.assertIn("ssh__connect naming production hb", d[2])
        # no shell named: read as sh and as PowerShell, the stricter wins; a named shell picks one dialect
        self.assertEqual(guard._mcp_modes({"command": "x"}, "command"), ("Bash", "PowerShell"))
        self.assertEqual(guard._mcp_modes({"command": "x", "shell": "C:/Program Files/PowerShell/7/pwsh.exe"},
                                          "command"), ("PowerShell",))
        self.assertEqual(guard._mcp_modes({"command": "x", "shell": "/bin/bash"}, "command"), ("Bash",))
        self.assertEqual(guard._mcp_modes({"command": "x", "shell": "cmd"}, "command"), ("cmd",))
        self.assertEqual(guard.decide({"tool_name": DC + "start_process", "tool_input": {
            "command": "d^ocker --context ha ps", "shell": "cmd"}, "cwd": ROOT}, self.view)[:2], ("deny", "a"))
        # a production session an MCP terminal opens says that its later input is not checked
        d = guard.decide({"tool_name": DC + "start_process", "tool_input": {"command": "ssh pc"}}, self.view)
        self.assertIn("later is not checked) on production pc", d[2])
        # the second dialect never writes a second guard-warn.log line
        self.assertIsNone(guard.decide({"tool_name": DC + "start_process", "tool_input": {"command": "cargo test"},
                                        "cwd": ROOT}, self.view))
        self.assertEqual(len(self.warns()), 1)

    def test_long_install_path_keeps_the_reason(self):
        # a package unpacked under a long folder made the 160-char cap cut the reason itself ("in ssh to...")
        long = "bash C:/Users/someone/AppData/Local/Temp/very/long/install/folder/helper-node-orchestrator/scripts/orch"
        with mock.patch.object(guard, "ORCH", long):
            reason = guard._msg("local $(...) in ssh to helper ha", long + " run shell-investigate --node ha -- <cmd>")
        self.assertTrue(reason.startswith("blocked: local $(...) in ssh to helper ha; use: orch run "), reason)
        self.assertLessEqual(len(reason), 160, reason)

    def test_mf2_reasons(self):
        r = self.check("ssh pc \"$(cat cmd.txt)\"")
        self.assertIn("ssh to production pc (deploy@192.0.2.20) with a substitution the master's shell runs first",
                      r[2])
        self.assertIn("spell the remote command out", r[2])
        r = self.check("ssh pc docker logs web --since \"$(date +%s)\"")      # was allowed: read-only on its face
        self.assertEqual(r[:2], ("ask", "p"))
        self.assertIn("with a substitution the master's shell runs first", r[2])
        self.assertTrue(self.check("ssh ha \"$(cat c)\"")[2].startswith("blocked: local $(...) in ssh to helper ha"))
        self.assertIsNone(self.check("ssh pc 'echo $(date)'"))             # single quotes: it runs there
        # the substitution itself runs on the master and meets the local rules
        self.assertEqual(self.check("ssh pc \"echo $(docker system prune -af)\"")[:2], ("deny", "c"))

    def test_mf4_reasons(self):
        r = self.check("node -e \"require('child_process').execSync('ssh ha docker ps')\"")
        self.assertEqual(r, ("ask", "x", "inline node code naming helper ha needs your OK (the guard cannot read "
                                          "this code; spell the command out)"))
        self.assertIn("naming a protected path (.helper-orch)",
                      self.check("python -c \"import pathlib; (pathlib.Path.home() / '.helper-orch').exists()\"")[2])
        home = self.home.replace("\\", "/")
        self.assertIn("naming a protected path", self.check("python -c \"open('%s/x', 'w')\"" % home)[2])
        # a deny by another rule still wins over the ask
        self.assertEqual(self.check("python -c \"open('/home/m/.helper-orch/ALLOW_LOCAL','w')\"")[:2], ("deny", "e"))
        # a relative store path counts only inside that project
        self.assertIsNone(self.check("python -c \"print(open('.git/orch/config.json').read())\"", cwd=OUTSIDE))

    def test_context_messages(self):
        # full orch command, or its short name when a long install path needs the room
        self.assertRegex(self.check("docker context use orch-ha")[2], r"; use: (%s|orch) nodes$" % re.escape(ORCH))
        self.assertRegex(self.check("docker context rm orch-ha")[2], r"; use: (%s|orch) init$" % re.escape(ORCH))


class GuardProcess(unittest.TestCase):
    """End to end: the real hook process, stdin/stdout JSON, ORCH_HOME = a temp dir holding the sample view."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        shutil.copy(SAMPLE, os.path.join(self.home, "guard-view.json"))

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, payload, cwd=ROOT):
        if isinstance(payload, dict):
            payload = json.dumps(dict({"hook_event_name": "PreToolUse", "cwd": cwd}, **payload)).encode()
        env = dict(os.environ, ORCH_HOME=self.home)
        env.pop("ORCH_LABEL", None)
        p = subprocess.run([sys.executable, "-B", os.path.join(HERE, "guard.py")], input=payload,
                           capture_output=True, timeout=20, env=env)
        return p.returncode, p.stdout.decode(), p.stderr.decode()

    def decision(self, out):
        h = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(h["hookEventName"], "PreToolUse")
        return h["permissionDecision"], h["permissionDecisionReason"]

    def bash(self, cmd, cwd=ROOT, tool="Bash"):
        return self.call({"tool_name": tool, "tool_input": {"command": cmd}}, cwd)

    def test_deny_json(self):
        rc, out, _ = self.bash("docker system prune -af")
        self.assertEqual(rc, 0)
        d, r = self.decision(out)
        self.assertEqual(d, "deny")
        self.assertIn("orch", r)

    def test_ask_json(self):
        for cmd, reason in ((ORCH + " init --approve", "saving the orch setup needs your OK"),
                            ("ssh pc 'docker compose up -d'", "production pc")):
            rc, out, _ = self.bash(cmd)
            d, r = self.decision(out)
            self.assertEqual((rc, d), (0, "ask"), cmd)
            self.assertIn(reason, r)

    def test_powershell_deny(self):
        rc, out, _ = self.bash("dotnet test", tool="PowerShell")
        self.assertEqual(self.decision(out)[0], "deny")

    def test_cwd_scopes_rule_d(self):
        self.assertEqual(self.bash("npm ci", cwd=OUTSIDE)[:2], (0, ""))
        self.assertEqual(self.decision(self.bash("npm ci", cwd=ROOT + "/web")[1])[0], "deny")
        self.assertEqual(self.bash("npm ci", cwd=None)[:2], (0, ""))

    def test_allow_silent(self):
        self.assertEqual(self.bash("git status")[:2], (0, ""))
        self.assertEqual(self.bash("ssh pc docker ps")[:2], (0, ""))

    def test_other_tool_ignored(self):
        rc, out, _ = self.call({"tool_name": "Read", "tool_input": {"file_path": "docker build"}})
        self.assertEqual((rc, out), (0, ""))

    def test_fail_open_on_garbage(self):
        rc, out, err = self.call(b"{not json")
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("failing open", err)
        with open(os.path.join(self.home, "guard-errors.log")) as f:
            self.assertIn("fail-open", f.read())

    def test_write_protected_files_denied(self):
        for tool in ("Write", "Edit"):
            for path in (self.home + "/guard.json", self.home + "/trust.json", ROOT + "/.git/orch/config.json",
                         "C:\\ProgramData\\docker\\config\\daemon.json"):
                rc, out, _ = self.call({"tool_name": tool, "tool_input": {"file_path": path}})
                self.assertEqual(self.decision(out)[0], "deny", (tool, path))
        rc, out, _ = self.call({"tool_name": "Write", "tool_input": {"file_path": "hooks/test_guard.json"}})
        self.assertEqual(out, "")

    def test_no_view_no_decisions(self):
        os.remove(os.path.join(self.home, "guard-view.json"))
        for payload in ({"tool_name": "Bash", "tool_input": {"command": "docker system prune -af"}},
                        {"tool_name": "Bash", "tool_input": {"command": "ssh ha docker rm -f x"}},
                        {"tool_name": "Bash", "tool_input": {"command": ORCH + " init --approve"}},
                        {"tool_name": "PowerShell", "tool_input": {"command": "dotnet test"}},
                        {"tool_name": "Write", "tool_input": {"file_path": self.home + "/guard.json"}}):
            self.assertEqual(self.call(payload), (0, "", ""), payload)
        self.assertEqual(os.listdir(self.home), [])

    def test_corrupt_view(self):
        with open(os.path.join(self.home, "guard-view.json"), "w") as f:
            f.write("{oops")
        for cmd in ("docker system prune -af", "ssh pc docker ps", "npm ci", ORCH + " init --approve"):
            d, r = self.decision(self.bash(cmd)[1])
            self.assertEqual(d, "deny", cmd)
            self.assertTrue(r.startswith("blocked: guard view unreadable; use: ") and r.endswith(ORCH + " init"), r)
        rc, out, err = self.bash("git status")
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("guard view unreadable", err)
        self.assertEqual(self.bash(ORCH + " init")[:2], (0, ""))  # the fix itself passes
        rc, out, _ = self.call({"tool_name": "Write", "tool_input": {"file_path": self.home + "/trust.json"}})
        self.assertEqual(self.decision(out)[0], "deny")
        with open(os.path.join(self.home, "guard-errors.log")) as f:
            self.assertIn("guard view unreadable", f.read())

    def test_latency(self):
        t = time.perf_counter()
        self.bash("cd web && npm ci && dotnet test && ssh pc 'docker compose ps' && scp x hbx:/tmp")
        ms = (time.perf_counter() - t) * 1000
        sys.stderr.write("\nhook process run: %.0f ms\n" % ms)
        self.assertLess(ms, 1000)

    def test_empty_stdin(self):
        self.assertEqual(self.call(b"")[:2], (0, ""))

    def test_fix2_real_agent_commands(self):
        cmd = 'O="bash C:/work/skills/helper-node-orchestrator/scripts/orch"; $O init --approve | tail -1'
        self.assertEqual(self.decision(self.bash(cmd)[1]), ("ask", "saving the orch setup needs your OK"))
        d, r = self.decision(self.bash('X="ssh hbx"; $X docker rm -f web')[1])
        self.assertEqual(d, "deny")
        self.assertIn("production hb", r)
        d, r = self.decision(self.bash("python -m pytest -q", cwd="C:/work/lab/tests")[1])
        self.assertEqual(d, "deny")
        self.assertTrue(r.endswith(" run py-test"), r)
        self.assertEqual(self.bash("python -m pytest -q")[:2], (0, ""))   # C:/work/shop's jobs: warn only

    def test_fix2c_findings_end_to_end(self):
        # Q20 span blank, Q21 append, Q22 attached -m, Q23 wrapper, and host-variable substitution, via the hook
        d, _ = self.decision(self.bash("git commit -q -m docker_host; export DOCKER_HOST=ssh://ha; docker ps")[1])
        self.assertEqual(d, "deny")
        self.assertEqual(self.decision(self.bash("export DOCKER_HOST+=ssh://ha; docker ps")[1])[0], "deny")
        self.assertEqual(self.decision(self.bash("python -morch init --approve")[1]), ("ask",
                         "saving the orch setup needs your OK"))
        self.assertEqual(self.decision(self.bash("winpty ssh deploy@192.0.2.20 docker rm -f app")[1])[0], "deny")
        d, r = self.decision(self.bash("H=hbx; ssh $H docker rm -f web")[1])
        self.assertEqual(d, "deny")
        self.assertIn("production hb", r)
        self.assertEqual(self.bash("winpty ssh ha uptime")[:2], (0, ""))   # benign wrapped command stays silent

    def test_fix3_mcp_end_to_end(self):
        d, r = self.decision(self.call({"tool_name": DC + "start_process",
                                        "tool_input": {"command": "ssh pc docker rm -f web", "timeout_ms": 30000}})[1])
        self.assertEqual(d, "deny")
        self.assertIn("production pc", r)
        d, r = self.decision(self.call({"tool_name": DC + "interact_with_process",
                                        "tool_input": {"pid": 4242, "input": "docker --context orch-ha ps"}})[1])
        self.assertEqual(d, "deny")
        d, _ = self.decision(self.call({"tool_name": DC + "write_file",
                                        "tool_input": {"path": self.home + "/trust.json", "content": "{}"}})[1])
        self.assertEqual(d, "deny")
        d, r = self.decision(self.call({"tool_name": "mcp__ssh__connect", "tool_input": {"host": "hbx"}})[1])
        self.assertEqual(d, "ask")
        self.assertIn("production hb", r)
        for payload in ({"tool_name": DC + "list_directory", "tool_input": {"path": ROOT + "/src"}},
                        {"tool_name": DC + "start_process", "tool_input": {"command": "git status", "timeout_ms": 1}},
                        {"tool_name": "mcp__claude_ai_Google_Docs__read_doc", "tool_input": {"document_id": "a1"}}):
            self.assertEqual(self.call(payload)[:2], (0, ""), payload)

    def test_fix3_missing_view_fails_closed_once_trusted(self):
        os.remove(os.path.join(self.home, "guard-view.json"))
        open(os.path.join(self.home, "registry.json"), "w").close()   # orch init may write it before any approval
        self.assertEqual(self.bash("docker system prune -af"), (0, "", ""))
        open(os.path.join(self.home, "trust.json"), "w").close()      # an approved setup existed: fail closed
        for cmd in ("docker system prune -af", "ssh pc docker ps", "npm ci", ORCH + " init --approve",
                    "python -c \"import os; os.remove('guard-view.json')\""):
            d, r = self.decision(self.bash(cmd)[1])
            self.assertEqual(d, "deny", cmd)
            self.assertTrue(r.startswith("blocked: guard view unreadable; use: ") and r.endswith(ORCH + " init"), r)
        d, _ = self.decision(self.call({"tool_name": DC + "start_process",
                                        "tool_input": {"command": "docker ps", "timeout_ms": 1}})[1])
        self.assertEqual(d, "deny")
        rc, out, err = self.bash("git status")
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("guard-view.json is missing although trust.json exists", err)
        self.assertEqual(self.bash(ORCH + " init")[:2], (0, ""))     # the fix itself passes and writes the view
        rc, out, _ = self.call({"tool_name": "Write", "tool_input": {"file_path": self.home + "/trust.json"}})
        self.assertEqual(self.decision(out)[0], "deny")


if __name__ == "__main__":
    print("fixtures: %d (deny %d, ask %d, allow %d, mcp %d, new-heavy %d)"
          % (len(DENY) + len(ASK) + len(ALLOW) + len(MCP), len(DENY), len(ASK), len(ALLOW), len(MCP),
             len(NEW_HEAVY)))
    unittest.main(verbosity=2)
