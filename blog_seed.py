# blog_seed.py
"""Prefilled blog content for the Stella & Sage site.

Consumed by the one-time /admin/setup-blog endpoint in app.py: it creates the
"News" blog (if missing) and publishes these articles (skipping any whose
handle already exists, so re-running is safe).

The articles are written for SEO and AI crawlability: descriptive headings,
natural keywords (custom team stores, team fundraising, print on demand,
spirit wear), and internal links to key pages.
"""

BLOG_TITLE = "News"
BLOG_HANDLE = "news"
AUTHOR = "Stella & Sage Company"

SEED_ARTICLES = [
    {
        "handle": "welcome-to-stella-and-sage",
        "title": "Welcome to Stella & Sage Company: Custom Team Stores Made Simple",
        "tags": "team stores, custom merch, about us",
        "summary_html": "<p>Stella & Sage Company builds custom online merch stores for teams, schools, and organizations — no inventory, no minimums, with fundraising built in.</p>",
        "body_html": """
<p>Stella &amp; Sage Company is a <strong>veteran owned and operated</strong> platform that gives teams, schools, clubs, and organizations their own custom online merch store — without the headaches that usually come with team apparel.</p>

<h2>What we do</h2>
<p>Upload your logo once, and we build a complete storefront around it: tees, hoodies, tanks, youth sizes, and more, each one mocked up with your artwork and ready to order. There is <strong>no inventory to buy, no minimum orders, and nothing to ship yourself</strong> — every item is printed on demand and delivered straight to the person who ordered it.</p>

<h2>Who it's for</h2>
<ul>
  <li><strong>Youth sports teams</strong> — spirit wear for players and parents without a garage full of boxes</li>
  <li><strong>Schools and PTAs</strong> — school spirit wear with sizes for kids and adults</li>
  <li><strong>Clubs and nonprofits</strong> — branded merch that doubles as a fundraiser</li>
  <li><strong>Small businesses and creators</strong> — a merch line without upfront costs</li>
</ul>

<h2>Why teams choose Stella &amp; Sage</h2>
<p>Traditional team apparel means collecting sizes, fronting money for a bulk order, and hoping everything fits. A Stella &amp; Sage store flips that: every member shops for themselves, picks their own size and color, and pays directly. Admins manage the store from a simple dashboard — add products, adjust logo placement, choose garment colors, and invite members with a link or QR code.</p>

<p>And when your organization needs to raise money, our built-in <a href="/blogs/news/how-team-fundraising-works">fundraising tools</a> add a contribution to every item sold, with payouts handled automatically.</p>

<h2>Get started</h2>
<p>Visit <a href="/">stellasageco.com</a> to create your store, or check the <a href="/pages/resources">Resources</a> page for guides. Questions? Our <a href="/pages/support">support team</a> is happy to help.</p>
""",
    },
    {
        "handle": "how-team-fundraising-works",
        "title": "How Team Fundraising Works on Stella & Sage",
        "tags": "fundraising, team fundraising, stripe, nonprofits",
        "summary_html": "<p>Every Stella & Sage store can run a fundraiser: a set amount from every item sold goes to your cause, with automatic weekly payouts through Stripe.</p>",
        "body_html": """
<p>Merch your supporters actually want to wear is one of the most effective fundraisers there is. Stella &amp; Sage builds fundraising directly into your team store, so every hoodie and tee sold moves your cause forward.</p>

<h2>How it works</h2>
<ol>
  <li><strong>Start a campaign.</strong> From your store's admin page, click <em>Start a Fundraiser</em> and name your cause — a season fund, a trip, new equipment, a charity.</li>
  <li><strong>Set the amount per item.</strong> Choose how many dollars from each sale go to the fundraiser. The price your supporters see already includes it — total transparency.</li>
  <li><strong>Connect the payout account.</strong> The cause connects its own bank account through <strong>Stripe</strong>, the payment platform behind Apple Pay and Shopify. Setup takes about five minutes.</li>
  <li><strong>Share your store.</strong> Every purchase automatically sets aside the fundraising amount. Supporters can see a live progress bar toward your goal.</li>
</ol>

<h2>Where the money goes</h2>
<p>Funds move <strong>directly from the store to the cause's bank account</strong> on an automatic weekly schedule. Stella &amp; Sage never holds or controls the funds — Stripe transfers them straight to the recipient. No invoices, no checks, no chasing anyone down.</p>

<h2>Why it beats traditional fundraisers</h2>
<ul>
  <li>No door-to-door selling or handling cash</li>
  <li>Supporters get something they'll actually use — quality apparel with your logo</li>
  <li>Progress is visible: stores show a live goal bar so everyone can rally</li>
  <li>It runs itself: printing, shipping, and payouts are all automatic</li>
</ul>

<p>Ready to raise money with merch? <a href="/">Create your store</a>, then start a fundraiser from your admin page. More guides live on our <a href="/pages/resources">Resources</a> page.</p>
""",
    },
    {
        "handle": "launch-your-team-store-in-minutes",
        "title": "How to Launch Your Team's Custom Merch Store in Minutes",
        "tags": "how it works, team stores, setup guide",
        "summary_html": "<p>From logo upload to live store: how Stella & Sage turns your artwork into a full custom merch store your whole team can shop.</p>",
        "body_html": """
<p>Setting up team merch used to mean weeks of back-and-forth with a print shop. Here's how it works on Stella &amp; Sage instead.</p>

<h2>Step 1 — Upload your logo</h2>
<p>One good logo file is all we need. Our system automatically places it on a full product lineup and generates realistic mockups, so your store looks professionally designed from the first minute.</p>

<h2>Step 2 — Review your store</h2>
<p>You get an admin dashboard where you can see every product with your logo on it. Want changes? The built-in <strong>product editor</strong> lets you drag your logo exactly where you want it, resize it, add a back design on select products, and pick up to three garment colors per item — with a live preview that matches what actually prints.</p>

<h2>Step 3 — Add more products</h2>
<p>The <em>Add Products</em> catalog lets you build additional items whenever you like: premium tri-blend tees, heavyweight cotton tees, youth sizes, pullover and zip hoodies, crewnecks, and tank tops. Each one becomes its own listing in your store.</p>

<h2>Step 4 — Invite your members</h2>
<p>Share your store with a link or QR code. Members sign in, and your store appears on their dashboard — they can shop anytime, in their own size, paying for their own order. You never collect money or sizes again.</p>

<h2>Step 5 — Optional: turn on fundraising</h2>
<p>Add a per-item contribution and every sale supports your cause, with <a href="/blogs/news/how-team-fundraising-works">automatic payouts through Stripe</a>.</p>

<p>That's the whole process — most stores go from logo to live the same day. Start yours at <a href="/">stellasageco.com</a>.</p>
""",
    },
    {
        "handle": "our-print-on-demand-product-lineup",
        "title": "Our Product Lineup: Premium Tees, Hoodies, Tanks & Youth Sizes",
        "tags": "products, print on demand, apparel",
        "summary_html": "<p>A tour of the garments behind every Stella & Sage store — from ultra-soft tri-blend tees to premium hoodies and racerback tanks, all printed on demand.</p>",
        "body_html": """
<p>Every Stella &amp; Sage store is built on garments we'd wear ourselves. Here's what your members can shop, all printed on demand with your logo.</p>

<h2>Tees</h2>
<ul>
  <li><strong>Unisex Tri-Blend Tee</strong> — a cult-favorite blend of cotton, polyester, and rayon with a broken-in feel right out of the bag. The soft staple every store starts with.</li>
  <li><strong>Heavyweight Garment-Dyed Tee</strong> — thick, structured cotton with that lived-in garment-dyed look.</li>
  <li><strong>Classic Staple Tee</strong> — an everyday cotton tee in a wide range of colors.</li>
</ul>

<h2>Hoodies &amp; fleece</h2>
<ul>
  <li><strong>Premium Pullover Hoodie</strong> — the team favorite for sidelines and school halls.</li>
  <li><strong>Crewneck Sweatshirt</strong> — clean, classic, and easy to layer.</li>
  <li><strong>Full-Zip Hoodie</strong> — for members who want their logo on something they can throw on anywhere.</li>
</ul>

<h2>Tanks</h2>
<ul>
  <li><strong>Women's Racerback Tank</strong> — lightweight and flattering for practices and summer events.</li>
  <li><strong>Men's Premium Tank</strong> — a sturdy warm-weather staple.</li>
</ul>

<h2>Youth sizes</h2>
<p>Kids are half the point of team gear. The <strong>Youth Staple Tee</strong> and <strong>Youth Garment-Dyed Hoodie</strong> bring the same quality to youth sizing, so the whole family can match on game day.</p>

<h2>Printed on demand, one at a time</h2>
<p>Every item is printed when it's ordered using direct-to-garment printing, then shipped straight to the buyer. That means no leftover boxes of unsold larges, no upfront bulk purchase, and no guessing sizes. Store admins can fine-tune each product — logo placement, garment colors (up to three per product), even front-and-back designs on select items.</p>

<p>See the lineup with your own logo on it — <a href="/">create your store</a> and we'll build it automatically.</p>
""",
    },
    {
        "handle": "print-on-demand-vs-bulk-ordering-for-teams",
        "title": "Print-On-Demand vs. Bulk Ordering: What's Better for Teams and Clubs?",
        "tags": "print on demand, bulk ordering, team merch, comparison",
        "summary_html": "<p>Bulk apparel orders mean upfront cash, size guessing, and leftover boxes. Here's why teams are switching to print-on-demand stores instead.</p>",
        "body_html": """
<p>If you've ever run a team apparel order, you know the drill: collect sizes in a spreadsheet, front hundreds or thousands of dollars, wait weeks, then hand out shirts and discover three people quit and two need a different size. There's a better way.</p>

<h2>The bulk-order problem</h2>
<ul>
  <li><strong>Upfront cost:</strong> someone — usually a coach or parent — pays before anyone commits.</li>
  <li><strong>Size roulette:</strong> guess wrong and you eat the leftovers.</li>
  <li><strong>One-shot design:</strong> mid-season additions and new members are out of luck.</li>
  <li><strong>Admin burden:</strong> collecting cash, tracking orders, distributing boxes.</li>
</ul>

<h2>The print-on-demand alternative</h2>
<p>With a Stella &amp; Sage store, each item is printed when a member orders it and shipped directly to them. That changes the math completely:</p>
<ul>
  <li><strong>Zero upfront cost</strong> — nobody fronts money for the group.</li>
  <li><strong>Every size, always available</strong> — from youth small to adult 3XL, all season long.</li>
  <li><strong>New members welcome anytime</strong> — the store never closes.</li>
  <li><strong>No admin work</strong> — members pay for their own orders; printing and shipping are automatic.</li>
</ul>

<h2>What about price per item?</h2>
<p>Bulk pricing can beat print-on-demand on raw unit cost — if you sell every single piece. Factor in leftover inventory, wrong sizes, and volunteer hours, and on-demand usually wins for teams under a few hundred people. Plus, with <a href="/blogs/news/how-team-fundraising-works">built-in fundraising</a>, each sale can fund the team instead of draining it.</p>

<h2>The bottom line</h2>
<p>Bulk ordering made sense when it was the only option. For most teams, schools, and clubs today, a print-on-demand store means less risk, less work, and happier members. Try it with your own logo at <a href="/">stellasageco.com</a> — setup takes minutes and costs nothing.</p>
""",
    },
]
