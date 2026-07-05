# blog_seed.py
"""Prefilled blog content for the Stella & Sage site.

Consumed by /admin/setup-blog in app.py:
- creates the blog if missing
- creates any missing articles
- with ?update=1, also refreshes existing articles with the latest content

Links inside articles use the {{BLOG_URL}} placeholder, which the endpoint
replaces with the real blog path at publish time — so cross-article links
work no matter what the blog's handle is.
"""

BLOG_TITLE = "News"
BLOG_HANDLE = "news"
AUTHOR = "Stella & Sage Company"

SEED_ARTICLES = [
    {
        "handle": "welcome-to-stella-and-sage",
        "title": "Custom Team Stores Without the Chaos: How Stella & Sage Works",
        "tags": "team stores, custom merch, about us, spirit wear",
        "summary_html": "<p>One logo in, a full merch store out. How Stella & Sage gives teams, schools, and clubs their own online store — no inventory, no minimums, no spreadsheets.</p>",
        "body_html": """
<p>If you've ever organized team apparel, you know the ritual: a sign-up sheet, a size spreadsheet, one brave parent fronting $800, and a garage full of boxes — two of which will sit there forever because somebody quit and somebody else "runs small."</p>

<p>We built Stella &amp; Sage Company because that ritual deserves to die.</p>

<h2>One logo. A whole store.</h2>
<p>Here's the entire setup process: you give us your logo. That's it. Our system builds a complete online store around it — soft tri-blend tees, heavyweight cotton tees, premium hoodies, tank tops, youth sizes — every product mocked up with your artwork, priced, and ready to order. What used to take weeks of back-and-forth with a print shop happens in minutes.</p>

<p>From there, your store's admin dashboard gives you real control without any technical skills:</p>
<ul>
  <li><strong>Move your logo</strong> — drag it exactly where you want it on any product, with a live preview that matches what actually prints</li>
  <li><strong>Pick your colors</strong> — up to three garment colors per product</li>
  <li><strong>Add products</strong> — build new items from our catalog whenever you want</li>
  <li><strong>Invite your people</strong> — share one link or QR code and members shop for themselves</li>
</ul>

<h2>Nobody buys inventory. Ever.</h2>
<p>Every item is printed when it's ordered and shipped directly to the person who ordered it. That one change eliminates almost everything painful about team merch:</p>
<ul>
  <li>No upfront bulk purchase — nobody fronts money for the group</li>
  <li>No size guessing — every member picks their own size, youth small through adult 3XL</li>
  <li>No distribution day — orders ship to each buyer's door</li>
  <li>No leftover boxes — because there's no inventory at all</li>
</ul>

<h2>Built for the people who actually run things</h2>
<p>Stella &amp; Sage is <strong>veteran owned and operated</strong>, and we built it for the team moms, coaches, boosters, and club presidents who do this work as volunteers. The whole platform is designed around one question: <em>could a busy parent run this from their phone in the school pickup line?</em> If the answer is ever no, we redesign it.</p>

<h2>And when you need to raise money…</h2>
<p>Every store has fundraising built in. Flip it on, and a set amount from every item sold goes to your cause — with payouts going straight to the cause's bank account automatically. Read exactly how it works in <a href="{{BLOG_URL}}/how-team-fundraising-works">How Team Fundraising Works on Stella &amp; Sage</a>.</p>

<h2>See it with your own logo</h2>
<p>The fastest way to understand it is to see your own artwork on a full product line. <a href="/">Start your store</a> — it costs nothing to set up — or reach out through our <a href="/pages/contact">support page</a> and a real human will walk you through it.</p>
""",
    },
    {
        "handle": "how-team-fundraising-works",
        "title": "How Team Fundraising Works on Stella & Sage (No Cash Envelopes, No Chasing Checks)",
        "tags": "fundraising, team fundraising, stripe, nonprofits, boosters",
        "summary_html": "<p>Turn your team store into a fundraiser: set an amount per item, connect the cause's bank through Stripe, and every sale moves the goal bar. Here's exactly how the money flows.</p>",
        "body_html": """
<p>Most team fundraisers have a dirty secret: the thing being sold is junk nobody wants. Overpriced cookie dough. Discount cards for restaurants nobody visits. Wrapping paper in July.</p>

<p>Merch is different. A hoodie with your team's logo is something supporters <em>already want to buy</em> — the fundraiser is just the reason to finally do it.</p>

<h2>The whole flow, in four steps</h2>

<h3>1. Start a campaign from your store</h3>
<p>On your store's admin page, hit <strong>Start a Fundraiser</strong>. Name the cause — new uniforms, a tournament trip, a season fund, a local charity — and set an optional goal amount so supporters can watch progress climb.</p>

<h3>2. Choose the amount per item</h3>
<p>You decide how many dollars from every sale go to the fundraiser — say, $4 per item. Prices in your store update to include it, so there are no surprise fees and no awkward "donation upsell" at checkout. Supporters just buy merch; the fundraising happens automatically.</p>

<h3>3. Connect the cause's bank — takes about five minutes</h3>
<p>The money needs somewhere to go, and it shouldn't be a volunteer's personal Venmo. The cause connects its own bank account through <strong>Stripe</strong> — the same payment company behind Apple Pay and Shopify. Stella &amp; Sage never holds or touches the funds.</p>

<h3>4. Share the store and watch the bar move</h3>
<p>Every purchase automatically sets aside the fundraising amount, and your store shows a live progress bar toward the goal. Stripe transfers the funds <strong>directly to the cause's bank account on an automatic weekly schedule</strong>. No invoices. No checks. No one chasing anyone down at practice.</p>

<h2>Why this beats the classic fundraiser playbook</h2>
<ul>
  <li><strong>No product risk:</strong> nothing is bought until a supporter orders it — printing and shipping are on-demand</li>
  <li><strong>No cash handling:</strong> every dollar moves electronically and traceably</li>
  <li><strong>Supporters get real value:</strong> quality apparel they'll wear all year — walking advertising for your program</li>
  <li><strong>Transparency sells:</strong> a public goal bar turns buyers into recruiters ("we're $300 away — share the link!")</li>
  <li><strong>It never ends unless you want it to:</strong> the store stays open all season, so late joiners still contribute</li>
</ul>

<h2>Common questions</h2>
<p><strong>Who can be the recipient?</strong> Any cause with a bank account — a booster club, a school program, a nonprofit, a team fund.</p>
<p><strong>Can supporters see how much goes to the cause?</strong> You choose the visibility. Stores always show that a fundraiser is active; you decide whether the live total is public.</p>
<p><strong>What does Stella &amp; Sage take from the fundraiser?</strong> The fundraising amount you set belongs to the cause. We're a technology platform — funds move through Stripe directly from store to recipient.</p>
<p><strong>Can we run merch without a fundraiser?</strong> Absolutely. Fundraising is a switch, not a requirement.</p>

<p>Ready to try it? <a href="/">Create your store</a>, add your logo, and flip on a fundraiser from the admin page. New to the platform? Start with <a href="{{BLOG_URL}}/welcome-to-stella-and-sage">how Stella &amp; Sage works</a>.</p>
""",
    },
    {
        "handle": "launch-your-team-store-in-minutes",
        "title": "From Logo to Live Store in an Afternoon: A Setup Walkthrough",
        "tags": "how it works, team stores, setup guide, tutorial",
        "summary_html": "<p>A step-by-step walkthrough of launching a Stella & Sage team store — uploading your logo, tuning products in the editor, inviting members, and going live the same day.</p>",
        "body_html": """
<p>"How long does setup take?" is the first question every coach and club president asks. Honest answer: the automated part takes minutes, and most stores are live — with members shopping — the same afternoon. Here's the whole journey.</p>

<h2>Step 1 — Upload your logo (2 minutes)</h2>
<p>One good image file is all we need — ideally a PNG with a transparent background. Our system analyzes your logo's shape and automatically places it on a full product lineup: it knows a wide wordmark belongs across the chest and a tall mascot crest sits differently than a badge.</p>
<p>Don't have a logo yet? Our <a href="/pages/resources">Resources page</a> has a logo helper to get you something you'll be proud of.</p>

<h2>Step 2 — Walk your store (5 minutes)</h2>
<p>You'll get an admin dashboard showing every product with your artwork already on it, as realistic mockups. Most admins are surprised how little they want to change — but everything is changeable.</p>

<h2>Step 3 — Fine-tune with the product editor (as long as you like)</h2>
<p>Hit <strong>Edit</strong> on any product and you're in our visual editor:</p>
<ul>
  <li><strong>Drag and resize</strong> your logo anywhere in the print area — what you see is what prints</li>
  <li><strong>Swap artwork</strong> from your image library, or upload something new</li>
  <li><strong>Pick up to three garment colors</strong> — preview each one on the mockup before committing</li>
  <li><strong>Add a back design</strong> on supported products for that pro sports look</li>
</ul>
<p>Hit <em>Add to Store</em> and our automation rebuilds the product with your changes — new mockups, updated listing, done in a couple of minutes.</p>

<h2>Step 4 — Add more products (optional)</h2>
<p>The <strong>Add Products</strong> tab is a catalog of premium blanks — tri-blend tees, heavyweight garment-dyed tees, hoodies, crewnecks, full-zips, tanks, youth sizes. Tap one, place your art, pick colors, and it becomes a brand-new listing in your store.</p>

<h2>Step 5 — Invite your members (30 seconds)</h2>
<p>Your store is a <strong>private member storefront</strong>. Hit <em>Share Store</em> and you get a link and a QR code. Send the link in the group chat, or put the QR on a flyer at sign-ups. Members create an account, and your store appears on their dashboard — they shop their own sizes and pay for their own orders, forever.</p>

<h2>Step 6 — (Optional) Turn on fundraising</h2>
<p>One more switch and every sale contributes to your cause — <a href="{{BLOG_URL}}/how-team-fundraising-works">here's exactly how the money flows</a>.</p>

<h2>What you never have to do</h2>
<p>No inventory purchases. No size collection. No money collection. No trips to the post office. Printing and shipping happen automatically per order, straight to each buyer.</p>

<p>That's the whole thing. <a href="/">Start your store now</a> — and if you get stuck anywhere, <a href="/pages/contact">our support team</a> actually answers.</p>
""",
    },
    {
        "handle": "our-print-on-demand-product-lineup",
        "title": "The Stella & Sage Product Lineup: What Your Members Can Actually Order",
        "tags": "products, print on demand, apparel, hoodies, tees",
        "summary_html": "<p>A guided tour of every garment in a Stella & Sage store — what it feels like, who it's for, and why we chose it — from cult-favorite tri-blends to youth hoodies.</p>",
        "body_html": """
<p>Print-on-demand has a reputation problem: people picture thin, scratchy shirts with cracked prints. So let's be specific about what's actually in a Stella &amp; Sage store — because we chose every garment the same way you would: <em>would we wear this ourselves?</em></p>

<h2>The tees</h2>

<h3>Unisex Tri-Blend Tee — the store staple</h3>
<p>A cult favorite for a reason. The cotton/polyester/rayon blend has that broken-in, buttery feel straight out of the bag, drapes well on every body type, and holds print color beautifully. If your store sells one thing, it'll be this.</p>

<h3>Heavyweight Garment-Dyed Tee</h3>
<p>Thick, structured cotton with the lived-in, vintage look that's everywhere right now. Boxier fit, substantial feel — the tee for people who hate flimsy tees.</p>

<h3>Classic Staple Tee</h3>
<p>The dependable everyday cotton tee in a huge range of colors. Great when you want the lowest price point for a large group.</p>

<h2>The fleece</h2>

<h3>Premium Pullover Hoodie</h3>
<p>The item parents actually fight over. Soft fleece interior, sturdy exterior, prints beautifully front and (on supported setups) back. Sideline essential from October through March.</p>

<h3>Crewneck Sweatshirt</h3>
<p>Clean and classic — the one that looks right at the office <em>and</em> at practice. A logo on a crewneck instantly looks intentional.</p>

<h3>Full-Zip Hoodie</h3>
<p>For the members who live in layers. Front-panel printing on a zip is a different look — more team-staff, less team-fan — and people love it.</p>

<h2>The tanks</h2>
<p>The <strong>Women's Racerback Tank</strong> is light and flattering for summer practices and tournaments; the <strong>Men's Premium Tank</strong> is its sturdier counterpart. Both take a center-chest logo beautifully.</p>

<h2>The youth lineup</h2>
<p>Team gear that skips the kids misses the point. The <strong>Youth Staple Tee</strong> and <strong>Youth Garment-Dyed Hoodie</strong> bring the same fabrics to youth sizes, so the whole family matches on game day. (Fair warning from experience: the youth hoodie sells out grandparents' wallets fast.)</p>

<h2>How printing works</h2>
<p>Every piece is printed on demand with direct-to-garment (DTG) printing — ink bonded into the fabric, not a plasticky transfer sitting on top. Your admin controls placement and garment colors per product, and the mockup you approve is what prints. No minimums, no leftover stock, ever.</p>

<p>Want to see this lineup wearing <em>your</em> logo? <a href="/">Create your store</a> and it builds automatically — then read <a href="{{BLOG_URL}}/launch-your-team-store-in-minutes">the setup walkthrough</a> to make it yours.</p>
""",
    },
    {
        "handle": "print-on-demand-vs-bulk-ordering-for-teams",
        "title": "Print-On-Demand vs. Bulk Ordering for Team Merch: The Honest Math",
        "tags": "print on demand, bulk ordering, team merch, comparison",
        "summary_html": "<p>Bulk pricing looks cheaper per shirt — until you count leftovers, wrong sizes, and volunteer hours. An honest comparison for teams, schools, and clubs.</p>",
        "body_html": """
<p>Let's have the honest conversation, because bulk ordering isn't always wrong — it's just wrong for most teams, most of the time. Here's the real math.</p>

<h2>Where bulk ordering wins</h2>
<p>Credit where due: if you order 500 identical shirts and sell every single one, your per-unit cost beats print-on-demand. Screen printing at volume is cheap. If you're outfitting a 400-person corporate event with one design in one colorway, bulk is your friend.</p>

<h2>Where bulk ordering quietly bleeds you</h2>
<p>For a team, school, or club, the bulk order comes with costs that never make it into the quote:</p>
<ul>
  <li><strong>The float.</strong> Someone pays up front — often a coach or parent — and waits weeks to be made whole. Some never fully are.</li>
  <li><strong>The leftovers.</strong> Industry rule of thumb: order 10–15% extra "to be safe." That safety stock usually becomes a box in someone's garage. At $12/shirt × 30 leftover shirts, that's $360 of dead money on a single order.</li>
  <li><strong>The size lottery.</strong> Kids grow. Adults are optimistic. Wrong sizes either get eaten or trigger a painful exchange scramble.</li>
  <li><strong>The latecomers.</strong> New members join in week 3. Your options: place a tiny expensive reorder or tell a 9-year-old there's no jersey shirt for them.</li>
  <li><strong>The volunteer tax.</strong> Collecting sizes, chasing payments, sorting boxes, distributing orders — easily 10–20 volunteer hours per order. Free labor isn't free; it's why nobody wants to run merch next season.</li>
</ul>

<h2>How print-on-demand changes the equation</h2>
<p>With a Stella &amp; Sage store, each item is printed when a member orders it and shipped straight to their door:</p>
<ul>
  <li><strong>$0 upfront</strong> — there is no group buy to finance</li>
  <li><strong>Zero leftovers</strong> — nothing is printed until it's sold</li>
  <li><strong>Every size, all season</strong> — youth small to adult 3XL, always in stock</li>
  <li><strong>Late joiners welcome</strong> — the store never closes</li>
  <li><strong>Zero admin</strong> — members pay for their own orders; fulfillment is automatic</li>
</ul>

<h2>The break-even question</h2>
<p>Per shirt, on-demand costs a few dollars more than a perfectly-executed bulk order. But perfectly-executed bulk orders are rare. Once you count leftover stock, size mistakes, and the hours volunteers pour in, on-demand comes out ahead for most groups under a few hundred people — before you even factor in that a <a href="{{BLOG_URL}}/how-team-fundraising-works">built-in fundraiser</a> can turn every sale into money <em>coming in</em> instead of going out.</p>

<h2>A simple rule of thumb</h2>
<p><strong>One design, one deadline, guaranteed headcount?</strong> Bulk can work. <strong>A season, a community, changing rosters, families buying on their own schedule?</strong> That's a store, not an order — and that's what we build. <a href="/">See it with your logo</a>.</p>
""",
    },
    {
        "handle": "spirit-wear-ideas-that-actually-sell",
        "title": "Spirit Wear That Actually Sells: 9 Ideas From Real Team Stores",
        "tags": "spirit wear, team merch, ideas, boosters, school spirit",
        "summary_html": "<p>The spirit wear playbook: what teams and schools actually sell the most of, which products surprise people, and small tweaks that double a store's orders.</p>",
        "body_html": """
<p>After watching what actually gets ordered across team stores, some clear patterns emerge — and a few of them surprise every admin. If you're setting up spirit wear for a team, school, or club, steal these.</p>

<h2>1. The hoodie is your bestseller. Price it with confidence.</h2>
<p>Hoodies outsell everything from fall through spring. Parents will happily pay for quality fleece with their kid's team on it — don't race to the bottom on price, especially with a <a href="{{BLOG_URL}}/how-team-fundraising-works">fundraiser amount</a> built in.</p>

<h2>2. Youth sizes aren't optional</h2>
<p>Stores with youth tees and youth hoodies consistently outsell adult-only stores — kids want to match the team, and grandparents are the most reliable buyers on the roster.</p>

<h2>3. Give the parents their own gear</h2>
<p>"Hockey Mom," "Baseball Dad," booster-club versions — parents don't want to wear a player jersey; they want their own identity in team colors. A parent-oriented design is free money for a fundraiser.</p>

<h2>4. Two or three garment colors beat one</h2>
<p>Offering your main color plus a neutral (black, heather grey, or white) noticeably lifts orders — some people simply won't wear teal, and that's fine. Stella &amp; Sage lets you offer up to three colors per product.</p>

<h2>5. Tanks carry your summer</h2>
<p>Racerbacks and premium tanks turn summer practices and tournament weekends into merch season. Seasonal products keep the store feeling alive.</p>

<h2>6. The back print is the pro move</h2>
<p>Logo on the front, something on the back — a motto, a roster year, an established date. Back designs make merch feel like <em>real</em> team gear instead of a printed blank.</p>

<h2>7. QR codes at real-world moments</h2>
<p>The single best sales moment is sign-up night and the first game. Put your store's QR code (built into your share tools) on the folding table, and watch orders roll in from the bleachers.</p>

<h2>8. Announce restocks that aren't restocks</h2>
<p>On-demand stores never sell out — but attention fades. Adding one new product mid-season ("Crewnecks just dropped") re-sends everyone to the store, where they buy other things too.</p>

<h2>9. Show the goal bar</h2>
<p>If you're fundraising, make the progress public. "We're 70% to new uniforms" turns every supporter into a recruiter and gives people a reason to buy <em>now</em> instead of someday.</p>

<h2>Put it to work</h2>
<p>Every idea here is a checkbox, not a project, on a Stella &amp; Sage store: youth sizes, extra colors, back designs, QR sharing, and a live fundraiser bar are all built in. <a href="/">Start your store</a> and set it up in an afternoon — <a href="{{BLOG_URL}}/launch-your-team-store-in-minutes">here's the walkthrough</a>.</p>
""",
    },
    {
        "handle": "what-makes-a-great-team-logo",
        "title": "What Makes a Logo Print Well on Merch (And What Ruins It)",
        "tags": "logo design, artwork tips, print quality",
        "summary_html": "<p>Why some logos look incredible on a hoodie and others fall apart — file types, transparency, contrast, and the fixes that take five minutes.</p>",
        "body_html": """
<p>Two teams upload logos. One store looks like licensed pro merchandise; the other looks like a bake-sale flyer. The difference is almost never talent — it's a handful of technical details anyone can get right.</p>

<h2>The big four</h2>

<h3>1. Transparent background (PNG)</h3>
<p>The #1 issue we see: a logo saved as a JPG with a white box around it. On a white shirt you get away with it; on navy or heather grey, the white box prints too. Export as a <strong>PNG with a transparent background</strong> and your logo sits directly on the fabric like it belongs there.</p>

<h3>2. Resolution: bigger than you think</h3>
<p>A logo that looks fine as a 200-pixel avatar turns to mush at chest-print size. Aim for at least <strong>1500 pixels on the longest side</strong> — more is better. If your only copy is tiny, whoever made it can usually re-export it large in minutes.</p>

<h3>3. Contrast against fabric</h3>
<p>Navy logo on a navy hoodie: invisible. Think about the garment colors your members will pick and make sure your logo has an outline or fill that survives them. This is exactly why we let admins preview every color combination in the editor before anything prints.</p>

<h3>4. Detail that survives distance</h3>
<p>Hairline strokes and 6-point text disappear at bleacher distance. The best team logos read clearly from thirty feet: bold shapes, chunky outlines, one or two strong colors. Squint at your logo — whatever vanishes, simplify.</p>

<h2>Things that quietly ruin prints</h2>
<ul>
  <li><strong>Drop shadows and soft glows</strong> — often print as faint gray halos around the art</li>
  <li><strong>Screenshots of logos</strong> — compression artifacts get printed in full fidelity</li>
  <li><strong>Watermarked draft files</strong> — yes, the watermark prints too</li>
  <li><strong>Pure-white detail on light shirts</strong> — invisible on white and natural garments</li>
</ul>

<h2>What our system does for you</h2>
<p>Upload your logo and Stella &amp; Sage handles a lot automatically: we trim stray transparent padding so placement is exact, classify your logo's shape to auto-place it correctly per garment, and show you print-accurate mockups so you approve what will actually ship. The editor lets you drag, resize, and preview against every garment color — <a href="{{BLOG_URL}}/launch-your-team-store-in-minutes">here's how the whole flow works</a>.</p>

<h2>Don't have a logo yet?</h2>
<p>Our <a href="/pages/resources">Resources page</a> walks you through getting a store-ready logo, and <a href="/pages/contact">support</a> is happy to sanity-check your file before you build. Then <a href="/">put it on a store</a> and see it on twelve products at once.</p>
""",
    },
]
