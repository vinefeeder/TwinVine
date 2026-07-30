MOVIE = """
query Movie($id: ID!) {
  movie(id: $id) {
    id
    title
    shortName
    releaseYear
    synopsis
    duration
    mpaaRating
    underlyingId
    genres { name }
    labels { text }
    descriptiveAudio { available }
  }
}
"""

SERIES = """
query Series($id: ID!) {
  series(id: $id) {
    id
    title
    shortName
    shortDescription
    mpaaRating
    genres { name }
    labels { text }
    descriptiveAudio { available }
    seasons {
      nodes {
        id
        title
        number
        __typename
      }
      __typename
    }
    __typename
  }
}
"""

EPISODES = """
query Episodes($seasonId: ID!, $first: Int, $after: String) {
  episodes(seasonId: $seasonId, first: $first, after: $after) {
    pageInfo {
      endCursor
      hasNextPage
      __typename
    }
    nodes {
      id
      title
      releaseYear
      duration
      number
      description
      seasonNumber
      shortTitle
      rating { value }
      series {
        shortName
        __typename
      }
      __typename
    }
    __typename
  }
}
"""

EPISODE = """
query Episode($seriesShortName: String!, $seasonNumber: Int!, $episodeNumber: Int!) {
  episode(
    seriesShortName: $seriesShortName
    seasonNumber: $seasonNumber
    episodeNumber: $episodeNumber
  ) {
    id
    title
    releaseYear
    duration
    number
    description
    seasonNumber
    shortTitle
    rating { value }
    series {
      shortName
      __typename
    }
    __typename
  }
}
"""

PLAY = """
query Play($id: String!, $context: String, $behavior: BehaviorEnum, $supportedActions: [PlayFlowActionEnum!]!) {
  playFlow(id: $id, context: $context, behavior: $behavior, supportedActions: $supportedActions) {
    __typename
    ... on PlayContent {
      type
      continuationContext
      playheadPosition
      streams(types: [
        { packagingSystem: DASH, encryptionScheme: CBCS },
        { packagingSystem: DASH, encryptionScheme: CENC },
        { packagingSystem: HLS, encryptionScheme: NONE }
      ]) {
        id
        internalStreamId
        videoQuality { width height }
        widevine { authenticationToken licenseServerUrl }
        packagingSystem
        encryptionScheme
        playlistUrl
      }
      closedCaptions {
        vtt { location }
      }
      currentItem {
        content {
          __typename
          ... on Movie {
            id
            title
            underlyingId
            duration
          }
          ... on Episode {
            id
            title
            underlyingId
            duration
          }
        }
      }
      hints {
        videoId
        videoTitle
        resourceId
        isLive
      }
    }
    ... on ShowNotice {
      type
      actions {
        continuationContext
        text
      }
    }
    ... on ContinuePlay {
      type
    }
    ... on LogIn {
      type
    }
    ... on Noop {
      type
    }
  }
}
"""

WEB_PLAY = """
query PlayFlow($id: String!, $supportedActions: [PlayFlowActionEnum!]!, $context: String, $behavior: BehaviorEnum = DEFAULT, $streamTypes: [StreamDefinition!]) {
  playFlow(
    id: $id
    supportedActions: $supportedActions
    context: $context
    behavior: $behavior
  ) {
    ... on ShowNotice {
      type
      actions { continuationContext text }
      description
      title
      __typename
    }
    ... on PlayContent {
      type
      continuationContext
      heartbeatToken
      currentItem {
        content {
          __typename
          ... on Movie { id shortName amazonContentId duration }
          ... on Episode {
            id
            series { shortName __typename }
            seasonNumber
            number
            amazonContentId
            duration
          }
        }
      }
      amazonPlayback {
        playbackEnvelope
        playbackId
        __typename
      }
      playheadPosition
      closedCaptions {
        vtt { location __typename }
        __typename
      }
      streams(types: $streamTypes) {
        id
        playlistUrl
        packagingSystem
        encryptionScheme
        videoQuality { height width __typename }
        widevine { authenticationToken licenseServerUrl __typename }
        __typename
      }
      __typename
    }
    ... on ContinuePlay { type __typename }
    ... on LogIn { type __typename }
    ... on Noop { type __typename }
    __typename
  }
}
"""
