// Minimal DOM fixture for executing ChatGPT browser-relay DOM JavaScript in Node.js
// Supports element creation, querySelector/querySelectorAll, cloneNode, remove, innerText/textContent

class DOMNode {
    constructor(nodeType, tagName = '', attrs = {}, text = '') {
        this.nodeType = nodeType; // 1: Element, 3: Text
        this.tagName = tagName ? tagName.toUpperCase() : '';
        this.attributes = { ...attrs };
        this.children = [];
        this.parentNode = null;
        this._text = text;
    }

    get id() {
        return this.attributes.id || '';
    }

    set id(val) {
        this.attributes.id = String(val);
    }

    getAttribute(name) {
        return this.attributes[name] !== undefined ? String(this.attributes[name]) : null;
    }

    setAttribute(name, val) {
        this.attributes[name] = String(val);
    }

    hasAttribute(name) {
        return this.attributes[name] !== undefined;
    }

    removeAttribute(name) {
        delete this.attributes[name];
    }

    get textContent() {
        if (this.nodeType === 3) return this._text;
        return this.children.map(c => c.textContent).join('');
    }

    set textContent(val) {
        if (this.nodeType === 3) {
            this._text = val;
        } else {
            this.children = [new DOMNode(3, '', {}, val)];
            this.children[0].parentNode = this;
        }
    }

    get innerText() {
        return this.textContent;
    }

    set innerText(val) {
        this.textContent = val;
    }

    appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        return child;
    }

    removeChild(child) {
        const idx = this.children.indexOf(child);
        if (idx !== -1) {
            this.children.splice(idx, 1);
            child.parentNode = null;
        }
        return child;
    }

    remove() {
        if (this.parentNode) {
            this.parentNode.removeChild(this);
        }
    }

    focus() {
        if (this._ownerDocument) {
            this._ownerDocument.activeElement = this;
        }
    }

    contains(other) {
        let curr = other;
        while (curr) {
            if (curr === this) return true;
            curr = curr.parentNode;
        }
        return false;
    }

    getClientRects() {
        if (this.attributes && this.attributes['data-hidden'] !== undefined) {
            return [];
        }
        return [{}];
    }

    cloneNode(deep = true) {
        if (this.nodeType === 3) {
            return new DOMNode(3, '', {}, this._text);
        }
        const clone = new DOMNode(1, this.tagName, { ...this.attributes });
        if (deep) {
            for (const c of this.children) {
                clone.appendChild(c.cloneNode(true));
            }
        }
        return clone;
    }

    matches(selector) {
        return matchSelector(this, selector);
    }

    querySelector(selector) {
        const results = this.querySelectorAll(selector);
        return results.length > 0 ? results[0] : null;
    }

    querySelectorAll(selector) {
        const matches = [];
        const parts = selector.split(',').map(s => s.trim()).filter(Boolean);

        function traverse(curr) {
            for (const child of curr.children) {
                if (child.nodeType === 1) {
                    for (const p of parts) {
                        if (matchSimpleSelector(child, p)) {
                            matches.push(child);
                            break;
                        }
                    }
                    traverse(child);
                }
            }
        }

        traverse(this);
        return matches;
    }
}

function matchSimpleSelector(el, sel) {
    sel = sel.trim();
    if (!sel || el.nodeType !== 1) return false;

    // Class selector: .prose, .markdown
    if (sel.startsWith('.')) {
        const cls = sel.slice(1);
        const elClass = el.getAttribute('class') || '';
        return elClass.split(/\s+/).includes(cls);
    }

    // ID selector: #prompt-textarea
    if (sel.startsWith('#')) {
        return el.getAttribute('id') === sel.slice(1);
    }

    // Tag selector: main, article, button, svg
    if (/^[a-zA-Z0-9-]+$/.test(sel)) {
        return el.tagName.toLowerCase() === sel.toLowerCase();
    }

    // Attribute selector: [attr], [attr="val"], [attr*="val"], [attr^="val"], [attr$="val"]
    const attrMatch = sel.match(/^([a-zA-Z0-9-]+)?\[([a-zA-Z0-9_-]+)(?:([*^$]?=)(["']?)(.*?)\4)?\]$/);
    if (attrMatch) {
        const tag = attrMatch[1];
        const attr = attrMatch[2];
        const op = attrMatch[3];
        const val = attrMatch[5];
        if (tag && el.tagName.toLowerCase() !== tag.toLowerCase()) return false;
        const actual = el.getAttribute(attr);
        if (actual === null) return false;
        if (!op) return true;
        if (op === '=') return actual === val;
        if (op === '*=') return actual.includes(val);
        if (op === '^=') return actual.startsWith(val);
        if (op === '$=') return actual.endsWith(val);
    }

    return false;
}

function matchSelector(el, selector) {
    const parts = selector.split(',').map(s => s.trim()).filter(Boolean);
    return parts.some(p => matchSimpleSelector(el, p));
}

// Simple JSON to DOM builder
function buildDom(def) {
    if (typeof def === 'string') {
        return new DOMNode(3, '', {}, def);
    }
    const node = new DOMNode(1, def.tag || 'div', def.attrs || {});
    if (def.text) {
        node.appendChild(new DOMNode(3, '', {}, def.text));
    }
    node.dispatchEvent = () => {};
    if (Array.isArray(def.children)) {
        for (const childDef of def.children) {
            node.appendChild(buildDom(childDef));
        }
    }
    return node;
}

function runInDom(domDef, jsCode) {
    const docRoot = buildDom(domDef);
    const document = {
        activeElement: null,
        querySelector: (sel) => {
            if (docRoot.nodeType === 1 && matchSelector(docRoot, sel)) return docRoot;
            return docRoot.querySelector(sel);
        },
        querySelectorAll: (sel) => {
            const matches = docRoot.querySelectorAll(sel);
            if (docRoot.nodeType === 1 && matchSelector(docRoot, sel)) {
                return [docRoot, ...matches];
            }
            return matches;
        },
        execCommand: (cmd, showUI, value) => {
            if (cmd === 'insertText' && document.activeElement) {
                document.activeElement.innerText = value;
                return true;
            }
            if (cmd === 'selectAll' || cmd === 'delete') {
                if (cmd === 'delete' && document.activeElement) {
                    document.activeElement.innerText = '';
                }
                return true;
            }
            return false;
        }
    };

    function attachDoc(node) {
        node._ownerDocument = document;
        for (const c of node.children) {
            attachDoc(c);
        }
    }
    attachDoc(docRoot);

    const window = {
        document,
        location: { href: domDef.href || 'https://chatgpt.com/c/test' }
    };

    // Execute jsCode with document and window in scope
    const fn = new Function('document', 'window', `return (${jsCode});`);
    return fn(document, window);
}

// CLI runner via stdin
if (require.main === module) {
    let inputData = '';
    process.stdin.setEncoding('utf-8');
    process.stdin.on('data', chunk => { inputData += chunk; });
    process.stdin.on('end', () => {
        try {
            const payload = JSON.parse(inputData);
            const res = runInDom(payload.dom, payload.js);
            process.stdout.write(typeof res === 'string' ? res : JSON.stringify(res));
        } catch (err) {
            process.stderr.write(err.stack || String(err));
            process.exit(1);
        }
    });
}

module.exports = { DOMNode, buildDom, runInDom };
