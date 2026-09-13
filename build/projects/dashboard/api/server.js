// Sample service. Replace with a real one.
const http = require("http");
const port = process.env.PORT || 8080;
http.createServer((_req, res) => {
  res.writeHead(200, { "content-type": "application/json" });
  res.end(JSON.stringify({ service: "dashboard-api", ok: true }));
}).listen(port, () => console.log(`dashboard-api listening on ${port}`));
