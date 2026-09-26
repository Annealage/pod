# Commercial Licensing

Annealage Pod is [AGPL-3.0-only](LICENSE), which already allows commercial use. If you run pods in your own lab to develop and test your own products you don't need anything else, there's no fee and nothing to publish.

You'd want a commercial licence if you need to do something the AGPL only allows when you release your source, for example:

- shipping Pod (modified or not) inside a product you sell, like a test fixture, production programmer or lab instrument
- hosting a modified Pod as a service for other people
- distributing Pod combined with your own proprietary code

To get one, email **andrew@alelec.net** with your company, what you're planning to build or ship, and whether it involves redistributing, embedding or hosting Pod. If you're not sure whether the AGPL already covers what you're doing, just ask.

## Third-party components

A commercial licence only covers code I hold the copyright on. Third-party code keeps its own licence either way, and you'll need to meet its terms and keep its notices:

- the ARM CMSIS-DAP sources in `src/c_modules/dapprobe/vendor/cmsis-dap/`, and the port files derived from them, are Apache-2.0
- the Waveshare RP2350B board header, `src/boards/ANNEALAGE_POD_RP2350B/waveshare_rp2350b_plus_w.h`, is BSD-3-Clause
- the `src/micropython` submodule and what it pulls in (MicroPython, TinyUSB, lwIP, the Pico SDK, cyw43-driver, BTstack, ESP-IDF etc.) all have their own licences. Some restrict use, eg. cyw43-driver is only licensed for Raspberry Pi silicon.

[`REUSE.toml`](REUSE.toml) and the SPDX headers record the licence of every file.

## Hardware

Hardware designs are CERN-OHL-S-2.0, so you can build, modify and sell hardware from them as long as you publish your modified design sources. Commercial terms for closed derivatives are available the same way as for the software.

## Trademarks

"Annealage" and "Annealage Pod" are my trademarks, and none of the AGPL, CERN-OHL or a commercial licence grants any rights to them unless it explicitly says so.

It's fine to say your product is based on, or compatible with, Annealage Pod. It's not fine to sell hardware or software under those names, or anything likely to be confused with them, or in a way that implies I endorse it. If you distribute a modified version, rename it. Pods built by others from the published designs can't be sold as Annealage Pods.

## Contributions

Contributors sign off under the DCO and grant me the right to relicense their contribution, see [CONTRIBUTING.md](CONTRIBUTING.md). That's what allows contributed code to be included in a commercial licence; it's still available to everyone under the AGPL as well.
