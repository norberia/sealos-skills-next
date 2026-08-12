我们要在这个目录中创建一个新的 plugin，这个 plugin 的名字叫做 sealos-skills-next。

这个新的 plugin 必须符合 [https://agent-plugins.org/](https://agent-plugins.org/) 的规范。

我们这个 plugin 的本质上是对于 [https://github.com/labring/sealos-skills](https://github.com/labring/sealos-skills) 的重构。原先的这个 sealos-skills 的质量太烂太不稳定。你需要把这个项目临时 clone 下来，基于旧版的功能而不是格式或者流程，按照  [https://agent-plugins.org/](https://agent-plugins.org/) 的规范，完全从零开始重新构建这个 plugin。（虽然它叫 sealos-skills,但本质上其实是 plugin)

我们的 sealos-skills-next 对标的是 railway 的 plugin，因此我建议你也把 [https://github.com/railwayapp/railway-skills](https://github.com/railwayapp/railway-skills) clone 下来，我们参照它的格式来写我们这个 plugin。  
  
我们对这个 plugin 的期待是，它能够在 15 min 内成功部署绝大部分项目到 sealos。  
  
更关键的是，这个仓库（开发阶段项目库 3ba13bb21ab480fda599f7737dd0f460.csv）内的所有项目必须能够成功部署到 sealos 上。  
  
如果你认为有必要的话，我们甚至可以不再使用 sealos-deploy 这个命令 - 只要你能做到一句 ”帮我把 xxx 部署到 sealos 上“ 就能自动调用 skill，在 15 分钟内把项目成功、正确部署到 sealos 上的话。  
  
我们 sealos 是开源的，代码在这供你参考（/Users/che/Documents/GitHub/sealos）  
  
你只能在这个仓库内 clone 别的仓库，不要弄脏了我的其他文件。