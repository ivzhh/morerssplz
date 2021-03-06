from urllib.parse import urlencode, urljoin, quote
import json
import datetime
import logging
from functools import partial
import time

import tornado.httpclient
from tornado import web
import PyRSS2Gen
from lxml.html import fromstring, tostring

from . import base
from .zhihulib import fetch_zhihu, re_zhihu_img, tidy_content

logger = logging.getLogger(__name__)

ACCEPT_VERBS = ['MEMBER_CREATE_ARTICLE', 'ANSWER_CREATE']

class ZhihuAPI:
  baseurl = 'https://www.zhihu.com/api/v4/'
  user_agent = 'Mozilla/5.0 (X11; Linux x86_64; rv:63.0) Gecko/20100101 Firefox/63.0'

  async def activities(self, name):
    """
    Get user activities data from Zhihu API
    :param name (str): Zhihu user ID e.g., lilydjwg
    :return (dict): deserialized user data
    """
    url = 'members/%s/activities' % name
    query = {
      'desktop': 'True',
      'after_id': str(int(time.time())),
      'limit': '7',
    }
    url += '?' + urlencode(query)
    data = await self.get_json(url)
    return data

  async def topic(self, id, sort='hot'):
    """
    Get topic data from Zhihu API
    :param id (str): Zhihu topic ID e.g., 19551894
    :return (dict): deserialized topic data
    """
    url = ''
    if sort == 'hot':
      url = 'topics/%s/feeds/top_activity' % id
    elif sort == 'newest':
      url = 'topics/%s/feeds/timeline_activity' % id
    query = {
      'desktop': 'True',
      'after_id': str(int(time.time())),
      'limit': '7',
    }
    url += '?' + urlencode(query)
    data = await self.get_json(url)
    return data

  async def get_json(self, url):
    url = urljoin(self.baseurl, url)
    headers = {
      'User-Agent': self.user_agent,
      'Authorization': 'oauth c3cef7c66a1843f8b3a9e6a1e3160e20', # hard-coded in js
      'x-api-version': '3.0.40',
      'x-udid': 'AMAiMrPqqQ2PTnOxAr5M71LCh-dIQ8kkYvw=',
    }
    res = await fetch_zhihu(url, headers = headers)
    return json.loads(res.body.decode('utf-8'))

  async def card(self, name):
    """
    Zhihu member profile
    :param name: Zhihu member ID
    :return: dict - member's name, headline and url
    """
    url = 'https://www.zhihu.com/node/MemberProfileCardV2?params=%s' % (quote(
      json.dumps({
        'url_token': name,
      })))
    res = await fetch_zhihu(
      url, headers = {'User-Agent': self.user_agent})
    if not res.body:
      # e.g. https://www.zhihu.com/bei-feng-san-dai
      raise web.HTTPError(404)
    doc = fromstring(res.body.decode('utf-8'))
    name = doc.xpath('//span[@class="name"]')[0].text_content()
    url = doc.xpath('//a[@class="avatar-link"]')[0].get('href')

    # 知乎用户资料 - 一句话介绍
    tagline = doc.xpath('//div[@class="tagline"]')
    if tagline:
      headline = tagline[0].text_content()
    else:
      headline = ''

    return {
      'name': name,
      'headline': headline,
      'url': urljoin('https://www.zhihu.com/', url),
    }

  async def topic_info(self, id):
    """
    Zhihu topic information
    :param id (str): Zhihu topic id
    :return (dict): dict containing the topic's name, description and URL
    """

    url = urljoin('https://www.zhihu.com/topic/', id)

    resp = await fetch_zhihu(
      url, headers={'User-Agent': self.user_agent})
    if not resp.body:
      raise web.HTTPError(404)
    doc = fromstring(resp.body.decode('utf-8'))

    if doc.xpath('//*[contains(@class, "TopicMetaCard")]'):
      name = doc.xpath('//div[@class="TopicMetaCard-title"]')[0].text_content()
      desc = doc.xpath('//div[contains(@class, "TopicMetaCard-description")]')[0].text_content()
    elif doc.xpath('//*[contains(@class, "TopicCard")]'):
      name = doc.xpath('//h1[@class="TopicCard-titleText"]')[0].text_content()
      desc = doc.xpath('//div[@class="TopicCard-ztext"]')[0].text_content()
    # Unknown cases
    else:
      name = id
      desc = '未找到话题描述'

    return {
      'name': name,
      'description': desc,
      'url': url
    }

zhihu_api = ZhihuAPI()

async def activities2rss(name, digest=False, pic=None):
  info = await zhihu_api.card(name)
  url = info['url']
  info = {
    'title': '%s - 知乎动态' % info['name'],
    'description': info['headline'],
  }

  posts = []
  page = 0

  data = await zhihu_api.activities(name)
  posts = [x['target'] for x in data['data'] if x['verb'] in ACCEPT_VERBS]

  while len(posts) < 20 and page < 3:
    paging = data['paging']
    # logger.debug('paging: %r', paging)
    if paging['is_end']:
      break
    data = await zhihu_api.get_json(paging['next'])
    posts.extend(
      x['target'] for x in data['data'] if x['verb'] in ACCEPT_VERBS
    )
    page += 1

  rss = base.data2rss(
    url,
    info, posts,
    partial(post2rss, digest=digest, pic=pic),
  )
  xml = rss.to_xml(encoding='utf-8')
  return xml


def post_content(post, digest=False):
  content = ''

  # question preview has neither "excerpt" nor "content"
  if post['type'] == 'question':
    content = post['title']
  elif digest:
    content = post['excerpt']
  # Posts in Zhihu topics API response don't have the 'content' key by default
  # Although they can include it by carrying verbose query params
  # which are hard to maintain because Zhihu doesn't have public API documentation :(
  elif 'content' not in post:
    content = post['excerpt']
  else:
    content = post['content']

  return content


def post2rss(post, digest=False, pic=None, extra_types=()):
  """
  :param post (dict): 帖子数据
  :param digest (bool): 输出摘要
  :param pic (str): pic=cf 或 pic=google：指定图片代理提供方
  :param extra_types (tuple): 除回答和文章之外的其他帖子类型
  :return: PyRSS2Gen.RSSItem: post RSS item
  """
  if post['type'] == 'answer':
    title = '[回答] %s' % post['question']['title']
    url = 'https://www.zhihu.com/question/%s/answer/%s' % (
      post['question']['id'], post['id'])
    t_c = post['created_time']
    author = post['author']['name']

  elif post['type'] == 'article':
    title = '[文章] %s' % post['title']
    url = 'https://zhuanlan.zhihu.com/p/%s' % post['id']
    t_c = post['created']
    author = post['author']['name']

  elif 'question' in extra_types and post['type'] == 'question':
    title = '[问题] %s' % post['title']
    url = 'https://www.zhihu.com/question/%s' % (post['id'])
    t_c = post['created']
    author = None

  elif post['type'] in ['roundtable', 'live', 'column']:
    return

  else:
    logger.warn('unknown type: %s', post['type'])
    return

  content = post_content(post, digest)
  content = content.replace('<code ', '<pre><code ')
  content = content.replace('</code>', '</code></pre>')

  # Post only contains images but no text
  if not content:
    content = '<img src="%s">' % post.get('thumbnail')

  doc = fromstring(content)
  tidy_content(doc)
  if pic:
    base.proxify_pic(doc, re_zhihu_img, pic)
  content = tostring(doc, encoding=str)

  pub_date = datetime.datetime.utcfromtimestamp(t_c)

  item = PyRSS2Gen.RSSItem(
    title=title.replace('\x08', ''),
    link=url,
    guid=url,
    description=content.replace('\x08', ''),
    pubDate=pub_date,
    author=author,
  )
  return item


async def topic2rss(id, sort='hot', pic=None):
  info = await zhihu_api.topic_info(id)
  url = info.get('url')
  if sort == 'hot':
    title = '%s - 知乎话题 - 热门排序 ' % info.get('name')
  elif sort == 'newest':
    title = '%s - 知乎话题 - 时间排序 ' % info.get('name')
  info = {
    'title': title,
    'description': info.get('description')
  }

  page = 0
  data = await zhihu_api.topic(id, sort)
  posts = [x['target'] for x in data['data']]

  while len(posts) < 20 and page < 3:
    paging = data['paging']
    # logger.debug('paging: %r', paging)
    if paging['is_end']:
      break
    next_url = paging['next']
    if next_url.startswith('http://'):
      next_url = 'https://' + next_url[len('http://'):]
    data = await zhihu_api.get_json(next_url)
    posts.extend(
      x['target'] for x in data['data']
    )
    page += 1

  rss = base.data2rss(
    url,
    info, posts,
    # include question posts
    partial(post2rss, pic=pic, extra_types=('question'))
  )
  xml = rss.to_xml(encoding='utf-8')
  return xml

class ZhihuStream(base.BaseHandler):
  async def get(self, name):
    if name.endswith(' '):
      raise web.HTTPError(404)
    pic = self.get_argument('pic', None)
    digest = self.get_argument('digest', False) == 'true'

    rss = await activities2rss(name, digest=digest, pic=pic)
    self.finish(rss)

class ZhihuTopic(base.BaseHandler):
  async def get(self, id):
    """
    :param id (str): Zhihu topic id, as "19551894" in "https://www.zhihu.com/topic/19551894/hot"
    :return: Future with RSS content
    """
    if id.endswith(' '):
      raise web.HTTPError(404)
    sort = self.get_argument('sort', None)
    # invalid sort param
    if sort not in ('newest', 'hot'):
      # Sort by popularity by default
      sort = 'hot'
    pic = self.get_argument('pic', None)
    rss = await topic2rss(id, sort=sort, pic=pic)
    self.finish(rss)

async def test():
  # rss = await activities2rss('cai-qian-hua-56')
  rss = await activities2rss('farseerfc')
  print(rss)

if __name__ == '__main__':
  import tornado.ioloop
  from nicelogger import enable_pretty_logging
  enable_pretty_logging('DEBUG')
  loop = tornado.ioloop.IOLoop.current()
  loop.run_sync(test)
