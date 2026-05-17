function copyApiKey() {
    const apiKey = document.getElementById('apiKey');
    apiKey.select();
    apiKey.setSelectionRange(0, 99999); // mobile
    document.execCommand('copy');
    // Optional toast
    alert('API Key copied!');
}