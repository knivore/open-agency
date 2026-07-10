'use strict';

const proxyUrl = process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.https_proxy || process.env.http_proxy;

if (proxyUrl) {
    try {
        const {ProxyAgent, setGlobalDispatcher} = require('undici');
        setGlobalDispatcher(new ProxyAgent(proxyUrl));
        process.env.AGENCY_NODE_ONECLI_PROXY_ACTIVE = 'true';
    } catch (error) {
        process.env.AGENCY_NODE_ONECLI_PROXY_ACTIVE = 'false';
        if (process.env.AGENCY_NODE_ONECLI_PROXY_DEBUG === 'true') {
            console.warn(`[agency-onecli] Node proxy bootstrap failed: ${error.message}`);
        }
    }
}
