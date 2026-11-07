/** Purpose: hand a simulated ENS deployment to the controller's browser wallet.
 * Behavior: serve one reviewed transaction on loopback and require a matching account and chain.
 * Refs: institutional phase F03.2 and MetaMask eth_sendTransaction. Phase: F03.2.
 */
import { randomBytes } from 'node:crypto';
import { createServer } from 'node:http';
import { createPublicClient, http } from 'viem';
import { sepolia } from 'viem/chains';
import { prepareRegistry } from './ens-setup.mjs';

const client = createPublicClient({
  chain: sepolia, ccipRead: false,
  transport: http('https://ethereum-sepolia-rpc.publicnode.com', { timeout: 12000, retryCount: 2, retryDelay: 1500 }),
});
let proposal;
try {
  proposal = await prepareRegistry(client, process.argv[2], process.argv[3], process.argv[4]);
} catch (error) {
  console.error('ENS simulation failed: ' + (error.shortMessage ?? error.message));
  process.exit(1);
}
const nonce = randomBytes(24).toString('base64');
const attaching = proposal.action === 'attach_agent_subregistry';
const port = attaching ? 8794 : 8793;
const heading = attaching ? 'Attach the ORCA agent registry' : 'Deploy the ORCA agent registry';
const description = attaching
  ? 'Attach the verified registry to your ENS parent name. This enables registration of agent subnames.'
  : 'Deploy a registry with your wallet as registrar. Attaching the registry to your name comes after deployment.';
const host = `127.0.0.1:${port}`;
const page = `<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>${heading}</title>
<style nonce="${nonce}">body{font:1rem system-ui;max-width:46rem;margin:3rem auto;padding:1rem;color:#161616}button{background:#161616;color:white;padding:.8rem 1rem;font:inherit}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f3f3;padding:1rem}</style>
<main><h1>${heading}</h1>
<p>Sepolia testnet. ${description} The transaction transfers 0 ETH and uses testnet gas.</p>
<details open><summary>Review the simulated transaction</summary><pre id="proposal"></pre></details>
<button id="review" type="button">Review in MetaMask</button>
<p id="status" role="status"></p><pre id="hash" hidden></pre>
</main>
<script nonce="${nonce}">
const proposal = ${JSON.stringify(proposal).replaceAll('<', '\\u003c')};
const button = document.getElementById('review');
const status = document.getElementById('status');
document.getElementById('proposal').textContent = JSON.stringify(proposal, null, 2);
button.addEventListener('click', async () => {
  button.disabled = true;
  let requested = false;
  try {
    const wallet = window.ethereum;
    if (!wallet?.isMetaMask) throw new Error('Open this page in Chrome with MetaMask installed.');
    const accounts = await wallet.request({ method: 'eth_requestAccounts' });
    if (accounts[0]?.toLowerCase() !== proposal.controller.toLowerCase()) {
      throw new Error('Select the controller account shown above and retry.');
    }
    const chain = await wallet.request({ method: 'eth_chainId' });
    if (Number(chain) !== proposal.chain_id) throw new Error('Select Sepolia in MetaMask and retry.');
    status.textContent = 'Review the transaction in MetaMask.';
    requested = true;
    const hash = await wallet.request({ method: 'eth_sendTransaction', params: [proposal.transaction] });
    if (!/^0x[0-9a-f]{64}$/i.test(hash)) throw new Error('Check MetaMask Activity for the transaction before retrying.');
    status.textContent = 'Submitted. Share this public hash so the receipt can be verified.';
    const output = document.getElementById('hash');
    output.hidden = false;
    output.textContent = hash;
  } catch (error) {
    status.textContent = error.message || 'Check MetaMask Activity before retrying.';
    button.disabled = requested && error.code !== 4001;
    if (button.disabled) status.textContent += ' Check MetaMask Activity before retrying.';
  }
});
</script></html>`;

const server = createServer((request, response) => {
  if (request.method !== 'GET' || request.url !== '/' || request.headers.host !== host) {
    response.writeHead(404).end();
    return;
  }
  response.writeHead(200, {
    'Content-Type': 'text/html; charset=utf-8',
    'Cache-Control': 'no-store',
    'Content-Security-Policy': `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}'; frame-ancestors 'none'; base-uri 'none'`,
  }).end(page);
});
server.listen(port, '127.0.0.1', () => console.log(`http://${host}/`));
