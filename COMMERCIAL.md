# Commercial Licensing

Annealage Pod is open source under the [GNU Affero General Public License v3.0 only](LICENSE). The AGPL already permits commercial use: a company can run Pod in its own lab, on its own benches, to develop and test its own products, with no licence fee and no obligation to publish anything, as long as it doesn't distribute Pod or offer a modified Pod to others over a network.

A separate commercial licence exists for the cases where the AGPL's source-sharing terms don't fit.

## When you'd want a commercial licence

You need one only if you want to do something the AGPL doesn't allow without releasing your source. For example:

- Shipping Pod, or a modified Pod, inside a product you sell (a test fixture, a production programmer, a lab instrument) without publishing the corresponding source under the AGPL.
- Offering a modified Pod to third parties over a network, as a hosted service, without publishing your modifications.
- Combining Pod with proprietary code into a work you distribute, where the AGPL would require the combined work's source to be released.

If you can meet the AGPL's terms, you don't need a commercial licence.

## How to obtain one

Email **andrew@alelec.net** with your company name and country, a short description of what you intend to build or ship, and whether you intend to redistribute, embed, or host the software. You will receive a licensing proposal within a few business days. If you are unsure whether the AGPL already covers your use, ask; pre-clearance is free.

## Third-party components

The commercial licence covers only code whose copyright Andrew Leech holds. Third-party code in this repository stays under its own licence regardless of which licence you take Pod under:

- the vendored ARM CMSIS-DAP sources under `src/c_modules/dapprobe/vendor/cmsis-dap/`, and the port files derived from them, are Apache-2.0;
- the Waveshare RP2350B board header, `src/boards/ANNEALAGE_POD_RP2350B/waveshare_rp2350b_plus_w.h`, is BSD-3-Clause;
- the `src/micropython` submodule and the libraries it pulls in (MicroPython, TinyUSB, lwIP, the Pico SDK, cyw43-driver, BTstack, ESP-IDF and others) keep their own licences, some of which restrict use, for example cyw43-driver to Raspberry Pi silicon.

[`REUSE.toml`](REUSE.toml), the SPDX headers and [`LICENSES/`](LICENSES/) record the licence of every file. Whatever licence you take Pod under, you must meet those components' terms, including preserving their notices.

## Hardware

Hardware designs published in this repository are licensed under `CERN-OHL-S-2.0`. You may build, modify and sell hardware from them, provided you publish your modified design sources under the same licence. Commercial terms for closed derivatives of the hardware designs are available on the same basis as for the software.

## Trademarks

"Annealage" and "Annealage Pod" are trademarks of Andrew Leech. Neither the AGPL, the CERN-OHL nor a commercial licence grants trademark rights unless it says so explicitly.

You may say truthfully that your product is based on, or compatible with, Annealage Pod. You may not sell hardware or software under the Annealage or Annealage Pod names, or under names or branding likely to be confused with them, or in a way that suggests endorsement by or affiliation with Andrew Leech. If you distribute a modified version, rename it. Pods built and sold by third parties from the published designs must not be marketed as Annealage Pods.

## Contributions

Contributions are accepted under the terms in [CONTRIBUTING.md](CONTRIBUTING.md): a Developer Certificate of Origin sign-off, plus a grant to Andrew Leech of a licence to use and relicense the contribution under any terms, including commercial. That grant is what allows the commercial licence to include contributed code. Contributions are also available to everyone under the AGPL.
