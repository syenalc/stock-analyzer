# スマホからLAN経由でアクセスするためのStreamlit起動スクリプト
# PCと同じWi-Fi上のスマホから http://<このPCのIP>:8501 でアクセスできる

# 自分のIPアドレスを表示
Write-Host "===== ネットワーク情報 =====" -ForegroundColor Cyan
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.PrefixOrigin -eq 'Dhcp' -or $_.IPAddress -like '192.168.*' -or $_.IPAddress -like '10.*'
} | Select-Object -First 1).IPAddress

if ($ip) {
    Write-Host "スマホからアクセス: http://${ip}:8501" -ForegroundColor Yellow
} else {
    Write-Host "IPアドレス取得失敗。ipconfig で確認してください。" -ForegroundColor Red
}
Write-Host ""

# Streamlitを全インターフェースで起動
.venv\Scripts\activate
streamlit run dashboard/app.py --server.address=0.0.0.0 --server.port=8501
