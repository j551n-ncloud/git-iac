// Sample service. Replace with a real one.
const interval = Number(process.env.INTERVAL_MS || 60000);
setInterval(() => console.log(`dashboard-worker tick ${new Date().toISOString()}`), interval);
console.log(`dashboard-worker started, interval ${interval}ms`);
